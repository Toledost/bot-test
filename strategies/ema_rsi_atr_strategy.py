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
        atr_history_needed = self._cfg.atr_period
        if self._cfg.min_volatility_ratio > 0:
            atr_history_needed += self._cfg.volatility_sma_period
        return (
            max(
                self._cfg.ema_slow_period,
                self._cfg.rsi_period,
                atr_history_needed,
                self._cfg.trend_filter_ema_period,
            )
            + 2
        )

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

        # Filtro de tendencia opcional: solo toma señales a favor de la tendencia
        # de fondo (precio vs. EMA larga), para descartar cruces que son ruido de
        # corto plazo dentro de un pullback contra-tendencia. Desactivado por
        # defecto (trend_filter_ema_period=0) para no alterar el comportamiento
        # existente salvo que se configure explícitamente.
        allow_long, allow_short = True, True
        if self._cfg.trend_filter_ema_period > 0:
            trend_ema = ema(close, self._cfg.trend_filter_ema_period)
            current_trend_ema = float(trend_ema.iloc[-1])
            current_close = float(close.iloc[-1])
            allow_long = current_close > current_trend_ema
            allow_short = current_close < current_trend_ema
            indicators["trend_ema"] = current_trend_ema

        # Filtro de volatilidad mínima opcional: descarta señales cuando el ATR
        # actual está deprimido respecto a su propia media reciente (mercado en
        # compresión/chop), donde un SL basado en ATR queda tan ajustado que
        # cualquier mechazo de baja liquidez lo activa sin que haya movimiento
        # direccional real. Desactivado por defecto (min_volatility_ratio=0).
        allow_by_volatility = True
        if self._cfg.min_volatility_ratio > 0:
            atr_sma = atr_series.rolling(self._cfg.volatility_sma_period).mean()
            current_atr_sma = float(atr_sma.iloc[-1])
            allow_by_volatility = current_atr > current_atr_sma * self._cfg.min_volatility_ratio
            indicators["atr_sma"] = current_atr_sma

        if bullish_cross and current_rsi < self._cfg.rsi_overbought and allow_long and allow_by_volatility:
            reason = f"Cruce alcista EMA({ema_label}) con RSI={current_rsi:.2f}"
            logger.info(reason)
            return StrategyResult(signal=Signal.LONG, atr=current_atr, reason=reason, indicators=indicators)

        if bearish_cross and current_rsi > self._cfg.rsi_oversold and allow_short and allow_by_volatility:
            reason = f"Cruce bajista EMA({ema_label}) con RSI={current_rsi:.2f}"
            logger.info(reason)
            return StrategyResult(signal=Signal.SHORT, atr=current_atr, reason=reason, indicators=indicators)

        return StrategyResult(
            signal=Signal.HOLD, atr=current_atr, reason="Sin condiciones de entrada", indicators=indicators
        )
