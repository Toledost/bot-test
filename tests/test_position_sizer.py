"""Tests de la lógica crítica de dimensionamiento de riesgo."""
from __future__ import annotations

import pytest

from config.settings import RiskConfig
from core.exceptions import InsufficientBalanceError
from risk_management.position_sizer import PositionSizer


@pytest.fixture
def risk_cfg() -> RiskConfig:
    return RiskConfig(risk_per_trade_pct=1.0, daily_loss_limit_pct=2.0, max_open_positions=1)


def test_long_position_size_matches_fixed_risk(risk_cfg: RiskConfig) -> None:
    sizer = PositionSizer(risk_cfg)
    result = sizer.calculate(
        balance=1000.0,
        entry_price=100.0,
        atr=2.0,
        atr_sl_multiplier=1.5,
        atr_tp_multiplier=3.0,
        is_long=True,
    )

    # risk_amount = 1000 * 1% = 10; stop_distance = 2 * 1.5 = 3; amount = 10 / 3
    assert result.risk_amount == pytest.approx(10.0)
    assert result.amount == pytest.approx(10.0 / 3.0)
    assert result.stop_loss_price == pytest.approx(97.0)
    assert result.take_profit_price == pytest.approx(106.0)

    # La pérdida real al tocar el SL debe igualar el riesgo objetivo.
    loss_at_sl = (100.0 - result.stop_loss_price) * result.amount
    assert loss_at_sl == pytest.approx(result.risk_amount)


def test_short_position_reverses_sl_tp(risk_cfg: RiskConfig) -> None:
    sizer = PositionSizer(risk_cfg)
    result = sizer.calculate(
        balance=1000.0,
        entry_price=100.0,
        atr=2.0,
        atr_sl_multiplier=1.5,
        atr_tp_multiplier=3.0,
        is_long=False,
    )

    assert result.stop_loss_price == pytest.approx(103.0)
    assert result.take_profit_price == pytest.approx(94.0)


def test_zero_balance_raises(risk_cfg: RiskConfig) -> None:
    sizer = PositionSizer(risk_cfg)
    with pytest.raises(InsufficientBalanceError):
        sizer.calculate(
            balance=0.0,
            entry_price=100.0,
            atr=2.0,
            atr_sl_multiplier=1.5,
            atr_tp_multiplier=3.0,
            is_long=True,
        )


def test_zero_atr_raises(risk_cfg: RiskConfig) -> None:
    sizer = PositionSizer(risk_cfg)
    with pytest.raises(ValueError):
        sizer.calculate(
            balance=1000.0,
            entry_price=100.0,
            atr=0.0,
            atr_sl_multiplier=1.5,
            atr_tp_multiplier=3.0,
            is_long=True,
        )
