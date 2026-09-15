"""Tests del circuit breaker de pérdida diaria."""
from __future__ import annotations

from config.settings import RiskConfig
from risk_management.circuit_breaker import DailyLossCircuitBreaker


def _cfg() -> RiskConfig:
    return RiskConfig(risk_per_trade_pct=1.0, daily_loss_limit_pct=2.0, max_open_positions=1)


def test_breaker_trips_when_loss_exceeds_limit() -> None:
    breaker = DailyLossCircuitBreaker(_cfg(), starting_balance=1000.0)
    assert breaker.can_open_position() is True

    breaker.register_balance(985.0)  # -1.5%, dentro del límite
    assert breaker.can_open_position() is True

    breaker.register_balance(975.0)  # -2.5%, supera el límite de 2%
    assert breaker.can_open_position() is False


def test_breaker_stays_tripped_until_manually_reset_within_same_day() -> None:
    breaker = DailyLossCircuitBreaker(_cfg(), starting_balance=1000.0)
    breaker.register_balance(970.0)
    assert breaker.can_open_position() is False

    # Aunque el balance se recupere dentro del mismo día, el breaker permanece activado.
    breaker.register_balance(1000.0)
    assert breaker.can_open_position() is False
