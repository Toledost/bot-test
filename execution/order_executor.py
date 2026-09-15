"""Interfaz común de ejecución de órdenes (real vs. simulada)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from strategies.base_strategy import Signal


@dataclass(frozen=True)
class ExecutedTrade:
    symbol: str
    side: str  # "buy" | "sell"
    amount: float
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    sl_order_id: str | None
    tp_order_id: str | None


class OrderExecutor(ABC):
    """Contrato que deben cumplir tanto el ejecutor real como el simulado (paper).

    Ambas implementaciones garantizan que, tras abrir una posición, las
    órdenes de protección (SL/TP) quedan colocadas antes de retornar.
    """

    @abstractmethod
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
        raise NotImplementedError

    @abstractmethod
    def close_position(
        self, *, symbol: str, side: str, amount: float, position_side: str | None = None
    ) -> None:
        """Cierra o reduce una posición. `position_side` (LONG/SHORT) solo aplica
        en el ejecutor real bajo BingX Hedge Mode; el ejecutor de paper lo ignora.
        """
        raise NotImplementedError

    @abstractmethod
    def get_balance(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_open_positions(self) -> list[dict]:
        raise NotImplementedError
