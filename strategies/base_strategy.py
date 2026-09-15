"""Clase base abstracta para estrategias de trading."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class Signal(str, Enum):
    LONG = "long"
    SHORT = "short"
    CLOSE = "close"
    HOLD = "hold"


@dataclass(frozen=True)
class StrategyResult:
    """Resultado de evaluar la estrategia sobre el último cierre de vela."""

    signal: Signal
    atr: float | None = None
    reason: str = ""


class BaseStrategy(ABC):
    """Contrato que toda estrategia de trading debe implementar.

    Una estrategia recibe un DataFrame de velas OHLCV (columnas: timestamp,
    open, high, low, close, volume, ordenadas ascendentemente por tiempo) y
    devuelve una señal discreta. No conoce posiciones abiertas, balance ni
    ejecución: esa responsabilidad vive en risk_management/ y execution/.
    """

    name: str = "base_strategy"

    @abstractmethod
    def min_candles_required(self) -> int:
        """Cantidad mínima de velas necesarias para producir una señal válida."""
        raise NotImplementedError

    @abstractmethod
    def evaluate(self, ohlcv: pd.DataFrame) -> StrategyResult:
        """Evalúa el DataFrame de velas y retorna la señal resultante."""
        raise NotImplementedError
