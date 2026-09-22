"""Tests de detección de cierre (SL/trailing) y registro en el journal en paper trading."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from config.settings import CoreConfig, PaperConfig
from execution.paper_executor import PaperOrderExecutor
from strategies.base_strategy import Signal


@pytest.fixture
def executor(tmp_path: Path) -> PaperOrderExecutor:
    paper_cfg = PaperConfig(
        initial_balance=1000.0,
        taker_fee_pct=0.0,  # sin fees para que el PnL sea exacto y fácil de verificar
        slippage_pct=0.0,  # sin slippage por la misma razón
        db_path=str(tmp_path / "trades_paper.db"),
    )
    core_cfg = CoreConfig(
        loop_interval_seconds=30,
        max_retries=5,
        retry_backoff_base_seconds=2,
        journal_db_path=str(tmp_path / "journal.db"),
    )
    return PaperOrderExecutor(paper_cfg, core_cfg, str(tmp_path / "logs"), "test_strategy")


def test_poll_closed_trade_detects_stop_loss_hit_on_long(executor: PaperOrderExecutor) -> None:
    executor.open_position(
        symbol="BTC/USDT:USDT",
        signal=Signal.LONG,
        amount=1.0,
        entry_price_hint=100.0,
        stop_loss_price=95.0,
        take_profit_price=110.0,
        signal_reason="test entry",
        indicators={"rsi": 45.0},
    )

    # Precio todavía entre SL y TP: no debe cerrar.
    assert executor.poll_closed_trade("BTC/USDT:USDT", current_price_hint=100.0) is None

    # Precio cruza el SL hacia abajo: debe cerrar con pérdida.
    result = executor.poll_closed_trade("BTC/USDT:USDT", current_price_hint=94.0)
    assert result is not None
    assert result.close_reason == "stop_loss"
    assert result.pnl == pytest.approx(-5.0)  # (95 - 100) * 1.0
    assert executor.get_open_positions() == []


def test_poll_closed_trade_detects_stop_loss_hit_on_short(executor: PaperOrderExecutor) -> None:
    executor.open_position(
        symbol="BTC/USDT:USDT",
        signal=Signal.SHORT,
        amount=2.0,
        entry_price_hint=100.0,
        stop_loss_price=105.0,
        take_profit_price=90.0,
        signal_reason="test short entry",
        indicators={"rsi": 72.0},
    )

    # El precio baja a favor de la posición mucho más allá del antiguo TP: sin
    # Take Profit fijo, no debe cerrar hasta que el precio toque el SL.
    assert executor.poll_closed_trade("BTC/USDT:USDT", current_price_hint=89.0) is None

    result = executor.poll_closed_trade("BTC/USDT:USDT", current_price_hint=106.0)
    assert result is not None
    assert result.close_reason == "stop_loss"
    assert result.pnl == pytest.approx(-10.0)  # (100 - 105) * 2.0
    assert executor.get_balance() == pytest.approx(990.0)


def test_update_trailing_stop_moves_sl_in_favor_on_long(executor: PaperOrderExecutor) -> None:
    executor.open_position(
        symbol="BTC/USDT:USDT",
        signal=Signal.LONG,
        amount=1.0,
        entry_price_hint=100.0,
        stop_loss_price=95.0,
        take_profit_price=110.0,
        signal_reason="test entry",
        indicators={"rsi": 45.0},
    )

    # Canal de 3 velas cuyo mínimo (98.0) supera el SL inicial (95.0): debe subir el SL.
    ohlcv = pd.DataFrame(
        {
            "low": [98.0, 99.0, 101.0, 102.0],
            "high": [103.0, 104.0, 106.0, 107.0],
        }
    )
    executor.update_trailing_stop("BTC/USDT:USDT", ohlcv, channel_period=3)

    # channel_period=3 mira las 3 velas antes de la última (excluida): [98.0, 99.0, 101.0].
    open_trade = executor.get_open_positions()[0]
    assert open_trade["stop_loss_price"] == pytest.approx(98.0)


def test_update_trailing_stop_never_moves_sl_against_trader(executor: PaperOrderExecutor) -> None:
    executor.open_position(
        symbol="BTC/USDT:USDT",
        signal=Signal.LONG,
        amount=1.0,
        entry_price_hint=100.0,
        stop_loss_price=95.0,
        take_profit_price=110.0,
        signal_reason="test entry",
        indicators={"rsi": 45.0},
    )

    # Canal cuyo mínimo (90.0) está por debajo del SL vigente: no debe retroceder.
    ohlcv = pd.DataFrame({"low": [90.0, 91.0, 92.0], "high": [96.0, 97.0, 98.0]})
    executor.update_trailing_stop("BTC/USDT:USDT", ohlcv, channel_period=3)

    open_trade = executor.get_open_positions()[0]
    assert open_trade["stop_loss_price"] == pytest.approx(95.0)


def test_poll_closed_trade_returns_none_without_open_position(executor: PaperOrderExecutor) -> None:
    assert executor.poll_closed_trade("BTC/USDT:USDT", current_price_hint=100.0) is None


def test_journal_records_indicators_and_close_reason(tmp_path: Path, executor: PaperOrderExecutor) -> None:
    executor.open_position(
        symbol="ETH/USDT:USDT",
        signal=Signal.LONG,
        amount=1.0,
        entry_price_hint=50.0,
        stop_loss_price=48.0,
        take_profit_price=55.0,
        signal_reason="Cruce alcista EMA con RSI=40.00",
        indicators={"ema_fast": 50.5, "ema_slow": 49.8, "rsi": 40.0},
    )
    executor.poll_closed_trade("ETH/USDT:USDT", current_price_hint=47.5)

    import sqlite3

    conn = sqlite3.connect(tmp_path / "journal.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM journal_entries WHERE symbol = 'ETH/USDT:USDT'").fetchone()
    conn.close()

    assert row is not None
    assert row["status"] == "closed"
    assert row["close_reason"] == "stop_loss"
    assert "RSI=40.00" in row["signal_reason"]
    assert '"rsi": 40.0' in row["indicators_json"]
