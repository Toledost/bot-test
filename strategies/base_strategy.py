"""Clase base abstracta para estrategias de trading."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd


class Signal(str, Enum):
    LONG = "long"
    SHORT = "short"
    CLOSE = "close"
    HOLD = "hold"


@dataclass(frozen=True)
class StrategyResult:
    """Resultado de evaluar la estrategia sobre el último cierre de vela.

    `indicators` guarda los valores crudos que motivaron la señal (ej. valores
    de EMA/RSI en una estrategia de cruce), específicos de cada estrategia.
    Se persiste junto al trade para poder analizar después qué condiciones de
    mercado produjeron los mejores/peores resultados (ver scripts/analyze_performance.py).
    """

    signal: Signal
    atr: float | None = None
    reason: str = ""
    indicators: dict[str, float] = field(default_factory=dict)


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
