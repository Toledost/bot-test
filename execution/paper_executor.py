"""Ejecutor simulado (Dry-Run) para Paper Trading.

Consume datos de mercado reales de BingX pero nunca envía órdenes al exchange.
Simula comisiones taker y slippage conservador, y persiste balance/trades en SQLite.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from config.settings import PaperConfig
from execution.order_executor import ExecutedTrade, OrderExecutor
from execution.paper_store import PaperStore
from strategies.base_strategy import Signal
from utils.logger import get_logger

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

    def __init__(self, cfg: PaperConfig, log_dir: str) -> None:
        self._cfg = cfg
        self._store = PaperStore(cfg.db_path, cfg.initial_balance)
        self._open_trade_ids: dict[str, int] = {}
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
    ) -> ExecutedTrade:
        entry_side = "buy" if signal is Signal.LONG else "sell"
        fill_price = self._apply_slippage(entry_price_hint, entry_side)
        notional = fill_price * amount
        fee = self._fee(notional)

        balance = self._store.get_balance()
        new_balance = balance - fee
        self._store.set_balance(new_balance)

        opened_at = datetime.now(UTC).isoformat()
        trade_id = self._store.insert_open_trade(
            opened_at=opened_at,
            symbol=symbol,
            side=entry_side,
            amount=amount,
            entry_price=fill_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            fee_paid=fee,
        )
        self._open_trade_ids[symbol] = trade_id

        msg = (
            f"[PAPER] APERTURA {entry_side.upper()} {symbol} amount={amount:.6f} "
            f"fill={fill_price:.4f} SL={stop_loss_price:.4f} TP={take_profit_price:.4f} "
            f"fee={fee:.4f} balance={new_balance:.2f}"
        )
        logger.info(msg)
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
    ) -> None:
        # position_side (LONG/SHORT) es un concepto propio de BingX Hedge Mode en
        # el ejecutor real; el simulador de paper no lo necesita, se ignora aquí.
        exit_price_hint = exit_price_hint or 0.0
        exit_side = "sell" if side == "buy" else "buy"
        fill_price = self._apply_slippage(exit_price_hint, exit_side)
        notional = fill_price * amount
        fee = self._fee(notional)

        trade_id = self._open_trade_ids.get(symbol)
        pnl = 0.0
        if trade_id is not None:
            open_trades = {row["id"]: row for row in self._store.get_open_trades(symbol)}
            trade = open_trades.get(trade_id)
            if trade is not None:
                entry_price = float(trade["entry_price"])
                pnl = (
                    (fill_price - entry_price) * amount
                    if side == "buy"
                    else (entry_price - fill_price) * amount
                )
                self._store.close_trade(
                    trade_id,
                    closed_at=datetime.now(UTC).isoformat(),
                    exit_price=fill_price,
                    pnl=pnl,
                    extra_fee=fee,
                )

        balance = self._store.get_balance()
        new_balance = balance - fee + pnl
        self._store.set_balance(new_balance)
        self._open_trade_ids.pop(symbol, None)

        msg = (
            f"[PAPER] CIERRE {exit_side.upper()} {symbol} amount={amount:.6f} "
            f"fill={fill_price:.4f} pnl={pnl:.4f} fee={fee:.4f} balance={new_balance:.2f}"
        )
        logger.info(msg)
        _paper_trade_logger.info(msg)

    def get_balance(self) -> float:
        return self._store.get_balance()

    def get_open_positions(self) -> list[dict]:
        return [dict(row) for row in self._store.get_open_trades()]
