"""Estrategia de ejemplo: cruce de EMAs con filtro RSI y ATR para volatilidad."""
from __future__ import annotations

import pandas as pd

from config.settings import StrategyConfig
from strategies.base_strategy import BaseStrategy, Signal, StrategyResult
from strategies.indicators import atr, ema, rsi
from utils.logger import get_logger

logger = get_logger(__name__)


class EmaRsiAtrStrategy(BaseStrategy):
    """Señal LONG en cruce alcista de EMA rápida/lenta con RSI fuera de sobrecompra.

    Señal SHORT en cruce bajista con RSI fuera de sobreventa. El ATR se calcula
    siempre y se expone en el resultado para que risk_management defina los
    niveles de Stop Loss / Take Profit basados en volatilidad real del mercado.
    """

    name = "ema_rsi_atr"

    def __init__(self, cfg: StrategyConfig) -> None:
        self._cfg = cfg

    def min_candles_required(self) -> int:
        return max(self._cfg.ema_slow_period, self._cfg.rsi_period, self._cfg.atr_period) + 2

    def evaluate(self, ohlcv: pd.DataFrame) -> StrategyResult:
        if len(ohlcv) < self.min_candles_required():
            return StrategyResult(signal=Signal.HOLD, reason="Datos insuficientes para evaluar la estrategia")

        close = ohlcv["close"]

        ema_fast = ema(close, self._cfg.ema_fast_period)
        ema_slow = ema(close, self._cfg.ema_slow_period)
        rsi_series = rsi(close, self._cfg.rsi_period)
        atr_series = atr(ohlcv, self._cfg.atr_period)

        current_atr = float(atr_series.iloc[-1])

        # Cruce evaluado entre la penúltima y última vela cerrada, evitando
        # actuar sobre una vela en formación.
        prev_diff = ema_fast.iloc[-2] - ema_slow.iloc[-2]
        curr_diff = ema_fast.iloc[-1] - ema_slow.iloc[-1]
        current_rsi = float(rsi_series.iloc[-1])

        bullish_cross = prev_diff <= 0 and curr_diff > 0
        bearish_cross = prev_diff >= 0 and curr_diff < 0

        ema_label = f"{self._cfg.ema_fast_period}/{self._cfg.ema_slow_period}"
        indicators = {
            "ema_fast": float(ema_fast.iloc[-1]),
            "ema_slow": float(ema_slow.iloc[-1]),
            "rsi": current_rsi,
        }

        if bullish_cross and current_rsi < self._cfg.rsi_overbought:
            reason = f"Cruce alcista EMA({ema_label}) con RSI={current_rsi:.2f}"
            logger.info(reason)
            return StrategyResult(signal=Signal.LONG, atr=current_atr, reason=reason, indicators=indicators)

        if bearish_cross and current_rsi > self._cfg.rsi_oversold:
            reason = f"Cruce bajista EMA({ema_label}) con RSI={current_rsi:.2f}"
            logger.info(reason)
            return StrategyResult(signal=Signal.SHORT, atr=current_atr, reason=reason, indicators=indicators)

        return StrategyResult(
            signal=Signal.HOLD, atr=current_atr, reason="Sin condiciones de entrada", indicators=indicators
        )
