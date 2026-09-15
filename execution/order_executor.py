"""Interfaz común de ejecución de órdenes (real vs. simulada)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

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


@dataclass(frozen=True)
class ClosedTrade:
    """Resultado de una posición que se detectó cerrada (por SL, TP, o manual)."""

    symbol: str
    exit_price: float
    pnl: float
    close_reason: str  # "stop_loss" | "take_profit" | "manual"


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
        signal_reason: str = "",
        indicators: dict[str, float] | None = None,
        timestamp_hint: datetime | None = None,
    ) -> ExecutedTrade:
        """`timestamp_hint` permite inyectar el instante a registrar como apertura,
        en vez de usar el reloj real del sistema — necesario en scripts/backtest.py,
        donde cada "ciclo" corresponde a una vela histórica, no al momento real de
        ejecución del script. En producción (paper/live) se omite y se usa
        datetime.now(UTC) como siempre.
        """
        raise NotImplementedError

    @abstractmethod
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
        """Cierra o reduce una posición. `position_side` (LONG/SHORT) solo aplica
        en el ejecutor real bajo BingX Hedge Mode; el ejecutor de paper lo ignora.
        `timestamp_hint`: ver docstring de open_position.
        """
        raise NotImplementedError

    @abstractmethod
    def poll_closed_trade(
        self,
        symbol: str,
        current_price_hint: float | None = None,
        high_hint: float | None = None,
        low_hint: float | None = None,
        timestamp_hint: datetime | None = None,
    ) -> ClosedTrade | None:
        """Detecta si la posición gestionada por el bot en `symbol` ya se cerró.

        Se llama en cada ciclo antes de buscar nuevas señales. Retorna None si
        sigue abierta o si no hay ninguna posición gestionada por el bot.
        En paper, `current_price_hint` (el último precio de ticker del ciclo) es
        obligatorio: se usa para simular si el precio cruzó SL/TP. `high_hint`/
        `low_hint` son opcionales y solo los usa scripts/backtest.py: si se dan,
        se compara el SL/TP contra el rango completo de la vela (más realista
        que solo el cierre) en vez de contra `current_price_hint`. `timestamp_hint`:
        ver docstring de open_position. En live se ignoran todos: se verifica si
        las órdenes SL/TP siguen abiertas en el exchange.
        """
        raise NotImplementedError

    @abstractmethod
    def get_balance(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_open_positions(self) -> list[dict]:
        raise NotImplementedError
