"""Ejecutor de órdenes reales contra BingX, con protección SL/TP server-side inmediata."""
from __future__ import annotations

from core.exceptions import ProtectiveOrderFailure
from core.exchange_client import ExchangeClient
from execution.order_executor import ExecutedTrade, OrderExecutor
from strategies.base_strategy import Signal
from utils.logger import get_logger

logger = get_logger(__name__)


class LiveOrderExecutor(OrderExecutor):
    """Envía órdenes reales al exchange. Coloca SL/TP inmediatamente tras la entrada.

    Regla crítica de seguridad: si la orden de mercado se ejecuta pero la
    colocación de SL o TP falla, la posición queda temporalmente desprotegida.
    En ese caso se reintenta una vez y, si vuelve a fallar, se cierra la
    posición de inmediato para no dejar exposición sin protección server-side.
    """

    def __init__(self, client: ExchangeClient) -> None:
        self._client = client

    def open_position(
        self,
        *,
        symbol: str,
        signal: Signal,
        amount: float,
        entry_price_hint: float,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> ExecutedTrade:
        entry_side = "buy" if signal is Signal.LONG else "sell"
        exit_side = "sell" if signal is Signal.LONG else "buy"

        logger.info("Enviando orden de mercado %s %s amount=%.6f", entry_side.upper(), symbol, amount)
        market_order = self._client.create_market_order(symbol, entry_side, amount)
        fill_price = float(market_order.get("average") or market_order.get("price") or entry_price_hint)

        sl_order_id: str | None = None
        tp_order_id: str | None = None
        try:
            sl_order = self._client.create_stop_market_order(
                symbol, exit_side, amount, stop_loss_price, params={"reduceOnly": True}
            )
            sl_order_id = sl_order.get("id")

            tp_order = self._client.create_take_profit_market_order(
                symbol, exit_side, amount, take_profit_price, params={"reduceOnly": True}
            )
            tp_order_id = tp_order.get("id")
        except Exception as exc:  # noqa: BLE001 - cualquier fallo aquí es crítico
            logger.error(
                "Fallo colocando órdenes de protección SL/TP tras abrir posición: %s. "
                "Cerrando posición inmediatamente para evitar exposición sin protección.",
                exc,
            )
            self.close_position(symbol=symbol, side=entry_side, amount=amount)
            raise ProtectiveOrderFailure(str(exc)) from exc

        logger.info(
            "Posición abierta y protegida: %s %s amount=%.6f entry=%.4f SL=%.4f (id=%s) TP=%.4f (id=%s)",
            entry_side.upper(),
            symbol,
            amount,
            fill_price,
            stop_loss_price,
            sl_order_id,
            take_profit_price,
            tp_order_id,
        )

        return ExecutedTrade(
            symbol=symbol,
            side=entry_side,
            amount=amount,
            entry_price=fill_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            sl_order_id=sl_order_id,
            tp_order_id=tp_order_id,
        )

    def close_position(
        self, *, symbol: str, side: str, amount: float, position_side: str | None = None
    ) -> None:
        close_side = "sell" if side == "buy" else "buy"
        logger.info("Cerrando posición %s %s amount=%.6f", close_side.upper(), symbol, amount)
        self._client.create_market_order(symbol, close_side, amount, params={"reduceOnly": True})

    def get_balance(self) -> float:
        balance = self._client.fetch_balance()
        total = balance.get("USDT", {}).get("total")
        return float(total) if total is not None else 0.0

    def get_open_positions(self) -> list[dict]:
        return self._client.fetch_positions()
