"""Ejecutor de órdenes reales contra BingX, con protección SL/TP server-side inmediata."""
from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from core.exceptions import ProtectiveOrderFailure
from core.exchange_client import ExchangeClient
from execution.order_executor import ClosedTrade, ExecutedTrade, OrderExecutor
from strategies.base_strategy import Signal
from utils.logger import get_logger
from utils.trade_journal import TradeJournal

logger = get_logger(__name__)


class LiveOrderExecutor(OrderExecutor):
    """Envía órdenes reales al exchange. Coloca el SL inmediatamente tras la entrada.

    No hay Take Profit fijo: la salida es exclusivamente por Stop Loss, que
    solo se desplaza a favor del trader vía update_trailing_stop (canal
    Donchian), dejando correr la ganancia en vez de cortarla en un TP fijo.

    Regla crítica de seguridad: si la orden de mercado se ejecuta pero la
    colocación del SL falla, la posición queda temporalmente desprotegida.
    En ese caso se cierra la posición de inmediato para no dejar exposición
    sin protección server-side.
    """

    def __init__(self, client: ExchangeClient, journal: TradeJournal, strategy_name: str) -> None:
        self._client = client
        self._journal = journal
        self._strategy_name = strategy_name
        # Orden de protección (SL) de la posición actualmente gestionada por el
        # bot en cada símbolo. Necesario para poll_closed_trade: cuando este ID
        # ya no aparece entre las órdenes abiertas, la posición se cerró.
        self._managed_protective_orders: dict[str, tuple[str | None, str | None]] = {}
        self._journal_entry_ids: dict[str, int] = {}

    def open_position(
        self,
        *,
        symbol: str,
        signal: Signal,
        amount: float,
        entry_price_hint: float,
        stop_loss_price: float,
        take_profit_price: float,
        signal_reason: str = "",
        indicators: dict[str, float] | None = None,
        timestamp_hint: datetime | None = None,
    ) -> ExecutedTrade:
        # timestamp_hint es exclusivo de scripts/backtest.py: en live el reloj
        # real siempre es correcto, se ignora si llegara a pasarse. take_profit_price
        # se recibe por compatibilidad con la interfaz y se persiste a título
        # informativo en el journal, pero no se coloca orden ni se usa para cerrar.
        entry_side = "buy" if signal is Signal.LONG else "sell"
        exit_side = "sell" if signal is Signal.LONG else "buy"

        logger.info("Enviando orden de mercado %s %s amount=%.6f", entry_side.upper(), symbol, amount)
        market_order = self._client.create_market_order(symbol, entry_side, amount)
        fill_price = float(market_order.get("average") or market_order.get("price") or entry_price_hint)

        sl_order_id: str | None = None
        try:
            sl_order = self._client.create_stop_market_order(
                symbol, exit_side, amount, stop_loss_price, params={"reduceOnly": True}
            )
            sl_order_id = sl_order.get("id")
        except Exception as exc:  # noqa: BLE001 - cualquier fallo aquí es crítico
            logger.error(
                "Fallo colocando la orden de protección SL tras abrir posición: %s. "
                "Cerrando posición inmediatamente para evitar exposición sin protección.",
                exc,
            )
            self.close_position(symbol=symbol, side=entry_side, amount=amount)
            raise ProtectiveOrderFailure(str(exc)) from exc

        logger.info(
            "Posición abierta y protegida: %s %s amount=%.6f entry=%.4f SL=%.4f (id=%s)",
            entry_side.upper(),
            symbol,
            amount,
            fill_price,
            stop_loss_price,
            sl_order_id,
        )

        self._managed_protective_orders[symbol] = (sl_order_id, None)
        opened_at = datetime.now(UTC).isoformat()
        fee_paid = float(market_order.get("fee", {}).get("cost") or 0.0)
        journal_id = self._journal.record_open(
            execution_mode="live",
            strategy_name=self._strategy_name,
            symbol=symbol,
            side=entry_side,
            amount=amount,
            entry_price=fill_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            signal_reason=signal_reason,
            indicators=indicators or {},
            opened_at=opened_at,
            fee_paid=fee_paid,
        )
        self._journal_entry_ids[symbol] = journal_id

        return ExecutedTrade(
            symbol=symbol,
            side=entry_side,
            amount=amount,
            entry_price=fill_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            sl_order_id=sl_order_id,
            tp_order_id=None,
        )

    def close_position(
        self,
        *,
        symbol: str,
        side: str,
        amount: float,
        position_side: str | None = None,
        close_reason: str = "manual",
        timestamp_hint: datetime | None = None,
    ) -> None:
        close_side = "sell" if side == "buy" else "buy"
        logger.info("Cerrando posición %s %s amount=%.6f", close_side.upper(), symbol, amount)
        order = self._client.create_market_order(symbol, close_side, amount, params={"reduceOnly": True})
        self._finalize_journal_entry(symbol, order, close_reason)
        self._managed_protective_orders.pop(symbol, None)

    def poll_closed_trade(
        self,
        symbol: str,
        current_price_hint: float | None = None,
        high_hint: float | None = None,
        low_hint: float | None = None,
        timestamp_hint: datetime | None = None,
    ) -> ClosedTrade | None:
        """Verifica si la posición gestionada en `symbol` sigue abierta en BingX.

        La señal confiable de cierre es que la posición ya no aparezca (o tenga
        tamaño cero) en fetch_positions — no basta con mirar las órdenes SL/TP,
        porque BingX puede tardar en cancelar la orden opuesta tras ejecutar un
        trigger, dejándola temporalmente "abierta" aunque la posición ya cerró.
        Al confirmar el cierre, se cancela explícitamente cualquier orden de
        protección remanente para no dejar huérfanas apuntando a una posición
        que ya no existe.
        """
        protective_orders = self._managed_protective_orders.get(symbol)
        if protective_orders is None:
            return None

        positions = self._client.fetch_positions([symbol])
        position_still_open = any(abs(float(p.get("contracts") or 0)) > 0 for p in positions)
        if position_still_open:
            return None

        sl_order_id, tp_order_id = protective_orders
        for order_id in (sl_order_id, tp_order_id):
            if order_id is not None:
                self._cancel_orphan_order(order_id, symbol)

        journal_id = self._journal_entry_ids.pop(symbol, None)
        self._managed_protective_orders.pop(symbol, None)
        if journal_id is None:
            return None

        journal_row = self._journal.get_open_entry(symbol, execution_mode="live")
        if journal_row is None:
            return None

        entry_price = float(journal_row["entry_price"])
        amount = float(journal_row["amount"])
        side = str(journal_row["side"])
        is_long = side == "buy"

        # Sin Take Profit fijo, la única orden de protección es el SL: si la
        # posición se cerró, fue por ese trigger (o un cierre manual externo,
        # indistinguible de stop_loss sin más información del exchange).
        close_reason = "stop_loss"
        ticker = self._client.fetch_ticker(symbol)
        exit_price = float(ticker["last"])

        pnl = (exit_price - entry_price) * amount if is_long else (entry_price - exit_price) * amount
        closed_at = datetime.now(UTC).isoformat()

        self._journal.record_close(
            journal_id,
            closed_at=closed_at,
            exit_price=exit_price,
            pnl=pnl,
            extra_fee=0.0,
            close_reason=close_reason,
        )

        logger.info(
            "Cierre detectado en %s (positionSide protección ya no abierta): motivo=%s pnl≈%.4f",
            symbol,
            close_reason,
            pnl,
        )

        return ClosedTrade(symbol=symbol, exit_price=exit_price, pnl=pnl, close_reason=close_reason)

    def update_trailing_stop(
        self,
        symbol: str,
        ohlcv: pd.DataFrame,
        channel_period: int,
    ) -> None:
        """Desplaza el SL server-side según un canal Donchian, sin dejar la
        posición desprotegida: la nueva orden STOP_MARKET se coloca primero
        y solo tras confirmarse se cancela la vieja (ver regla 1 de gestión
        de riesgo: nunca una posición sin protección server-side).
        """
        protective_orders = self._managed_protective_orders.get(symbol)
        if protective_orders is None:
            return
        old_sl_order_id, _tp_order_id = protective_orders

        journal_row = self._journal.get_open_entry(symbol, execution_mode="live")
        if journal_row is None:
            return

        side = str(journal_row["side"])
        amount = float(journal_row["amount"])
        current_sl = float(journal_row["stop_loss_price"])
        is_long = side == "buy"
        exit_side = "sell" if is_long else "buy"

        channel = ohlcv.iloc[-(channel_period + 1) : -1]
        if channel.empty:
            return

        if is_long:
            candidate_sl = float(channel["low"].min())
            new_sl = max(candidate_sl, current_sl)
        else:
            candidate_sl = float(channel["high"].max())
            new_sl = min(candidate_sl, current_sl)

        if new_sl == current_sl:
            return

        try:
            new_sl_order = self._client.create_stop_market_order(
                symbol, exit_side, amount, new_sl, params={"reduceOnly": True}
            )
        except Exception as exc:  # noqa: BLE001 - no crítico: el SL vigente sigue protegiendo la posición
            logger.warning(
                "No se pudo colocar el nuevo SL del trailing en %s (SL vigente %.4f sigue activo): %s",
                symbol,
                current_sl,
                exc,
            )
            return

        new_sl_order_id = new_sl_order.get("id")
        if old_sl_order_id is not None:
            self._cancel_orphan_order(old_sl_order_id, symbol)

        self._managed_protective_orders[symbol] = (new_sl_order_id, None)
        self._journal.update_stop_loss(journal_row["id"], new_sl)
        logger.info(
            "[LIVE] TRAILING %s SL %.4f -> %.4f (orden %s)", symbol, current_sl, new_sl, new_sl_order_id
        )

    def _cancel_orphan_order(self, order_id: str, symbol: str) -> None:
        try:
            self._client.cancel_order(order_id, symbol)
        except Exception as exc:  # noqa: BLE001 - no crítico: puede ya no existir (ejecutada/expirada)
            logger.debug(
                "No se pudo cancelar orden de protección huérfana %s en %s: %s", order_id, symbol, exc
            )

    def _finalize_journal_entry(self, symbol: str, close_order: dict, close_reason: str) -> None:
        journal_id = self._journal_entry_ids.pop(symbol, None)
        if journal_id is None:
            return
        journal_row = self._journal.get_open_entry(symbol, execution_mode="live")
        if journal_row is None:
            return

        exit_price = float(close_order.get("average") or close_order.get("price") or 0.0)
        entry_price = float(journal_row["entry_price"])
        amount = float(journal_row["amount"])
        side = str(journal_row["side"])
        is_long = side == "buy"
        pnl = (exit_price - entry_price) * amount if is_long else (entry_price - exit_price) * amount
        fee_paid = float(close_order.get("fee", {}).get("cost") or 0.0)

        self._journal.record_close(
            journal_id,
            closed_at=datetime.now(UTC).isoformat(),
            exit_price=exit_price,
            pnl=pnl,
            extra_fee=fee_paid,
            close_reason=close_reason,
        )

    def get_balance(self) -> float:
        balance = self._client.fetch_balance()
        total = balance.get("USDT", {}).get("total")
        return float(total) if total is not None else 0.0

    def get_open_positions(self) -> list[dict]:
        return self._client.fetch_positions()
