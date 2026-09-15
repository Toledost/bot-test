"""Ejecutor simulado (Dry-Run) para Paper Trading.

Consume datos de mercado reales de BingX pero nunca envía órdenes al exchange.
Simula comisiones taker y slippage conservador, y persiste balance/trades en SQLite.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from config.settings import CoreConfig, PaperConfig
from execution.order_executor import ClosedTrade, ExecutedTrade, OrderExecutor
from execution.paper_store import PaperStore
from strategies.base_strategy import Signal
from utils.logger import get_logger
from utils.trade_journal import TradeJournal

logger = get_logger(__name__)

# Logger dedicado a trades_paper.log, separado del log general del bot.
_paper_trade_logger = logging.getLogger("paper_trades")


def _setup_paper_trade_logger(log_dir: str) -> None:
    if _paper_trade_logger.handlers:
        return
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(str(Path(log_dir) / "trades_paper.log"), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _paper_trade_logger.addHandler(handler)
    _paper_trade_logger.setLevel(logging.INFO)
    _paper_trade_logger.propagate = False


class PaperOrderExecutor(OrderExecutor):
    """Simula la ejecución de órdenes aplicando comisión taker y slippage conservador."""

    def __init__(
        self,
        cfg: PaperConfig,
        core_cfg: CoreConfig,
        log_dir: str,
        strategy_name: str,
        execution_mode: str = "paper",
    ) -> None:
        self._cfg = cfg
        self._strategy_name = strategy_name
        self._execution_mode = execution_mode
        self._store = PaperStore(cfg.db_path, cfg.initial_balance)
        self._journal = TradeJournal(core_cfg.journal_db_path)
        self._journal_entry_ids: dict[str, int] = {}
        if execution_mode == "paper":
            _setup_paper_trade_logger(log_dir)

    def _apply_slippage(self, price: float, side: str) -> float:
        """Aplica slippage conservador: siempre en contra del trader (peor precio de fill)."""
        slippage_factor = self._cfg.slippage_pct / 100
        if side == "buy":
            return price * (1 + slippage_factor)
        return price * (1 - slippage_factor)

    def _fee(self, notional: float) -> float:
        return notional * (self._cfg.taker_fee_pct / 100)

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
        entry_side = "buy" if signal is Signal.LONG else "sell"
        fill_price = self._apply_slippage(entry_price_hint, entry_side)
        notional = fill_price * amount
        fee = self._fee(notional)

        balance = self._store.get_balance()
        new_balance = balance - fee
        self._store.set_balance(new_balance)

        opened_at = (timestamp_hint or datetime.now(UTC)).isoformat()
        self._store.insert_open_trade(
            opened_at=opened_at,
            symbol=symbol,
            side=entry_side,
            amount=amount,
            entry_price=fill_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            fee_paid=fee,
        )

        journal_id = self._journal.record_open(
            execution_mode=self._execution_mode,
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
            fee_paid=fee,
        )
        self._journal_entry_ids[symbol] = journal_id

        tag = self._execution_mode.upper()
        msg = (
            f"[{tag}] APERTURA {entry_side.upper()} {symbol} amount={amount:.6f} "
            f"fill={fill_price:.4f} SL={stop_loss_price:.4f} TP={take_profit_price:.4f} "
            f"fee={fee:.4f} balance={new_balance:.2f}"
        )
        logger.info(msg)
        if self._execution_mode == "paper":
            _paper_trade_logger.info(msg)

        return ExecutedTrade(
            symbol=symbol,
            side=entry_side,
            amount=amount,
            entry_price=fill_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            sl_order_id="paper-sl",
            tp_order_id="paper-tp",
        )

    def close_position(
        self,
        *,
        symbol: str,
        side: str,
        amount: float,
        position_side: str | None = None,
        exit_price_hint: float | None = None,
        close_reason: str = "manual",
        timestamp_hint: datetime | None = None,
    ) -> None:
        # position_side (LONG/SHORT) es un concepto propio de BingX Hedge Mode en
        # el ejecutor real; el simulador de paper no lo necesita, se ignora aquí.
        tag = self._execution_mode.upper()
        exit_price_hint = exit_price_hint or 0.0
        exit_side = "sell" if side == "buy" else "buy"
        fill_price = self._apply_slippage(exit_price_hint, exit_side)
        notional = fill_price * amount
        fee = self._fee(notional)

        open_trades = self._store.get_open_trades(symbol)
        pnl = 0.0
        if open_trades:
            trade = open_trades[0]
            entry_price = float(trade["entry_price"])
            pnl = (
                (fill_price - entry_price) * amount if side == "buy" else (entry_price - fill_price) * amount
            )
            closed_at = (timestamp_hint or datetime.now(UTC)).isoformat()
            self._store.close_trade(
                trade["id"],
                closed_at=closed_at,
                exit_price=fill_price,
                pnl=pnl,
                extra_fee=fee,
            )

            journal_id = self._journal_entry_ids.pop(symbol, None)
            if journal_id is None:
                journal_row = self._journal.get_open_entry(symbol, execution_mode=self._execution_mode)
                journal_id = journal_row["id"] if journal_row else None
            if journal_id is not None:
                self._journal.record_close(
                    journal_id,
                    closed_at=closed_at,
                    exit_price=fill_price,
                    pnl=pnl,
                    extra_fee=fee,
                    close_reason=close_reason,
                )

        balance = self._store.get_balance()
        new_balance = balance - fee + pnl
        self._store.set_balance(new_balance)

        msg = (
            f"[{tag}] CIERRE ({close_reason}) {exit_side.upper()} {symbol} amount={amount:.6f} "
            f"fill={fill_price:.4f} pnl={pnl:.4f} fee={fee:.4f} balance={new_balance:.2f}"
        )
        logger.info(msg)
        if self._execution_mode == "paper":
            _paper_trade_logger.info(msg)

    def poll_closed_trade(
        self,
        symbol: str,
        current_price_hint: float | None = None,
        high_hint: float | None = None,
        low_hint: float | None = None,
        timestamp_hint: datetime | None = None,
    ) -> ClosedTrade | None:
        """Simula el cierre comparando el precio contra el SL/TP guardado.

        En paper trading en vivo, sin high_hint/low_hint, aproxima el cruce
        usando solo el último precio de ticker del ciclo (simplificación
        razonable para timeframes >= 1m). En backtest, high_hint/low_hint
        (el rango de la vela histórica) permiten detectar un cruce intravela
        que el precio de cierre solo no vería. Si la vela tocó tanto el SL
        como el TP, se asume conservadoramente que el SL ocurrió primero
        (peor caso: sin datos tick a tick no hay forma de saber el orden real).
        """
        if current_price_hint is None:
            return None

        open_trades = self._store.get_open_trades(symbol)
        if not open_trades:
            return None

        trade = open_trades[0]
        side = str(trade["side"])
        stop_loss_price = float(trade["stop_loss_price"])
        take_profit_price = float(trade["take_profit_price"])
        amount = float(trade["amount"])
        is_long = side == "buy"

        check_high = high_hint if high_hint is not None else current_price_hint
        check_low = low_hint if low_hint is not None else current_price_hint

        hit_sl = check_low <= stop_loss_price if is_long else check_high >= stop_loss_price
        hit_tp = check_high >= take_profit_price if is_long else check_low <= take_profit_price

        if not hit_sl and not hit_tp:
            return None

        close_reason = "stop_loss" if hit_sl else "take_profit"  # SL tiene prioridad en caso de ambigüedad
        exit_price_hint = stop_loss_price if hit_sl else take_profit_price

        balance_before = self._store.get_balance()
        self.close_position(
            symbol=symbol,
            side=side,
            amount=amount,
            exit_price_hint=exit_price_hint,
            close_reason=close_reason,
            timestamp_hint=timestamp_hint,
        )
        balance_after = self._store.get_balance()

        return ClosedTrade(
            symbol=symbol,
            exit_price=exit_price_hint,
            pnl=balance_after - balance_before,
            close_reason=close_reason,
        )

    def get_balance(self) -> float:
        return self._store.get_balance()

    def get_open_positions(self) -> list[dict]:
        return [dict(row) for row in self._store.get_open_trades()]
