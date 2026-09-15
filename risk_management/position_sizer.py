"""Dimensionamiento de lotaje según porcentaje fijo de riesgo por operación."""
from __future__ import annotations

from dataclasses import dataclass

from config.settings import RiskConfig
from core.exceptions import InsufficientBalanceError
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class PositionSizeResult:
    amount: float
    risk_amount: float
    stop_loss_price: float
    take_profit_price: float


class PositionSizer:
    """Calcula el tamaño de posición de forma que la pérdida al SL sea un % fijo del balance."""

    def __init__(self, cfg: RiskConfig) -> None:
        self._cfg = cfg

    def calculate(
        self,
        *,
        balance: float,
        entry_price: float,
        atr: float,
        atr_sl_multiplier: float,
        atr_tp_multiplier: float,
        is_long: bool,
    ) -> PositionSizeResult:
        if balance <= 0:
            raise InsufficientBalanceError(f"Balance inválido para dimensionar posición: {balance}")
        if atr <= 0:
            raise ValueError(f"ATR inválido para dimensionar posición: {atr}")

        risk_amount = balance * (self._cfg.risk_per_trade_pct / 100)
        stop_distance = atr * atr_sl_multiplier
        take_profit_distance = atr * atr_tp_multiplier

        if is_long:
            stop_loss_price = entry_price - stop_distance
            take_profit_price = entry_price + take_profit_distance
        else:
            stop_loss_price = entry_price + stop_distance
            take_profit_price = entry_price - take_profit_distance

        # amount * stop_distance = risk_amount  =>  amount = risk_amount / stop_distance
        amount = risk_amount / stop_distance

        logger.info(
            "Sizing: balance=%.2f risk=%.2f%% (%.2f) entry=%.4f stop_dist=%.4f amount=%.6f SL=%.4f TP=%.4f",
            balance,
            self._cfg.risk_per_trade_pct,
            risk_amount,
            entry_price,
            stop_distance,
            amount,
            stop_loss_price,
            take_profit_price,
        )

        return PositionSizeResult(
            amount=amount,
            risk_amount=risk_amount,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
        )
