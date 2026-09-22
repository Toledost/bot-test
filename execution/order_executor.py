"""Interfaz común de ejecución de órdenes (real vs. simulada)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

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

    Ambas implementaciones garantizan que, tras abrir una posición, la orden
    de protección (Stop Loss) queda colocada antes de retornar. No hay Take
    Profit fijo: la salida es exclusivamente por Stop Loss, que solo se
    desplaza a favor del trader vía update_trailing_stop (ver su docstring).
    `take_profit_price` se conserva en la interfaz por compatibilidad de
    sizing y se persiste a título informativo en el journal, pero ningún
    executor lo usa como criterio de cierre.
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
        obligatorio: se usa para simular si el precio cruzó el Stop Loss.
        `high_hint`/`low_hint` son opcionales y solo los usa scripts/backtest.py:
        si se dan, se compara el SL contra el rango completo de la vela (más
        realista que solo el cierre) en vez de contra `current_price_hint`.
        `timestamp_hint`: ver docstring de open_position. En live se ignoran
        todos: se verifica si la orden SL sigue abierta en el exchange.
        """
        raise NotImplementedError

    @abstractmethod
    def update_trailing_stop(
        self,
        symbol: str,
        ohlcv: pd.DataFrame,
        channel_period: int,
    ) -> None:
        """Actualiza el Stop Loss de la posición gestionada según un canal Donchian.

        En LONG, el SL sube al máximo entre el mínimo de las últimas
        `channel_period` velas (excluyendo la última, aún en formación/recién
        cerrada) y el SL actual — nunca retrocede a favor del mercado. En
        SHORT es simétrico con el máximo de esas velas. No hace nada si no
        hay posición gestionada en `symbol` o si el canal no mejora el SL
        vigente. Se llama una vez por ciclo, antes de evaluar nuevas señales.
        """
        raise NotImplementedError

    @abstractmethod
    def get_balance(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_open_positions(self) -> list[dict]:
        raise NotImplementedError
