"""Backtest de la estrategia activa sobre datos históricos reales de BingX.

En vez de esperar señales en tiempo real (lento: con TIMEFRAME=15m un cruce de
EMA puede tardar horas o días en aparecer), este script descarga N días de
velas históricas y corre la MISMA lógica de estrategia + riesgo + ejecución
simulada vela por vela, generando en segundos el volumen de trades que en vivo
tomaría semanas. Reutiliza EmaRsiAtrStrategy, PositionSizer y PaperOrderExecutor
tal cual están en producción — el backtest valida la estrategia real, no una
reimplementación aparte.

Los resultados se escriben en journal.db con execution_mode="backtest",
separados de los trades de paper trading en vivo (execution_mode="paper"), así
que nunca se mezclan. El balance simulado del backtest usa su propio archivo
SQLite (no toca data/trades_paper.db).

Limitación conocida: la señal de la estrategia se evalúa con las velas hasta
la vela actual (inclusive) y la entrada se ejecuta al cierre de esa misma
vela — una simplificación estándar de backtesting de velas cerradas, pero no
idéntica a la ejecución en vivo (que actúa un ciclo después de que la vela
cierra). El SL/TP sí se valida correctamente contra el high/low de cada vela
posterior, evitando el sesgo más grave (asumir que solo el cierre importa).

Uso:
    python scripts/backtest.py --days 180
    python scripts/backtest.py --days 30 --timeframe 5m
    python scripts/backtest.py --days 90 --reset   # borra resultados previos del backtest
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import CoreConfig, PaperConfig, load_settings  # noqa: E402
from core.exchange_client import ExchangeClient  # noqa: E402
from execution.paper_executor import PaperOrderExecutor  # noqa: E402
from risk_management.circuit_breaker import DailyLossCircuitBreaker  # noqa: E402
from risk_management.position_sizer import PositionSizer  # noqa: E402
from risk_management.validators import validate_max_open_positions, validate_sufficient_balance  # noqa: E402
from strategies.base_strategy import Signal  # noqa: E402
from strategies.ema_rsi_atr_strategy import EmaRsiAtrStrategy  # noqa: E402
from utils.trade_journal import TradeJournal  # noqa: E402

_OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
_MS_PER_DAY = 86_400_000


def _fetch_historical_ohlcv(
    client: ExchangeClient, symbol: str, timeframe: str, days: int
) -> pd.DataFrame:
    """Descarga `days` días de velas paginando con `since` (BingX limita a ~1000/request)."""
    since = int(time.time() * 1000) - days * _MS_PER_DAY
    all_candles: list[list[float]] = []

    print(f"Descargando {days} días de velas {timeframe} para {symbol}...")
    while True:
        batch = client.fetch_ohlcv(symbol, timeframe, limit=1000, since=since)
        if not batch:
            break
        all_candles.extend(batch)
        last_ts = int(batch[-1][0])
        if last_ts <= since:
            break  # el exchange dejó de avanzar, evita loop infinito
        since = last_ts + 1
        if last_ts >= int(time.time() * 1000) - 60_000:
            break  # ya llegamos a datos casi actuales
        print(f"  ...{len(all_candles)} velas descargadas hasta {pd.to_datetime(last_ts, unit='ms')}")

    df = pd.DataFrame(all_candles, columns=_OHLCV_COLUMNS).drop_duplicates(subset="timestamp")
    df = df.sort_values("timestamp").reset_index(drop=True)
    print(f"Total: {len(df)} velas descargadas.\n")
    return df


def run_backtest(days: int, timeframe: str | None, reset: bool) -> None:
    settings = load_settings()
    tf = timeframe or settings.exchange.timeframe

    backtest_paper_cfg = PaperConfig(
        initial_balance=settings.paper.initial_balance,
        taker_fee_pct=settings.paper.taker_fee_pct,
        slippage_pct=settings.paper.slippage_pct,
        db_path=str(Path(settings.paper.db_path).parent / "backtest_state.db"),
    )
    backtest_core_cfg = CoreConfig(
        loop_interval_seconds=settings.core.loop_interval_seconds,
        max_retries=settings.core.max_retries,
        retry_backoff_base_seconds=settings.core.retry_backoff_base_seconds,
        journal_db_path=settings.core.journal_db_path,
    )

    if reset:
        Path(backtest_paper_cfg.db_path).unlink(missing_ok=True)
        deleted = TradeJournal(backtest_core_cfg.journal_db_path).delete_by_mode("backtest")
        print(
            f"Estado previo del backtest eliminado ({backtest_paper_cfg.db_path}) "
            f"y {deleted} entradas 'backtest' borradas del journal.\n"
        )

    client = ExchangeClient(settings.exchange, settings.core)
    client.load_markets()
    ohlcv = _fetch_historical_ohlcv(client, settings.exchange.symbol, tf, days)

    strategy = EmaRsiAtrStrategy(settings.strategy)
    min_candles = strategy.min_candles_required()
    if len(ohlcv) < min_candles + 1:
        print(f"Datos insuficientes: se requieren al menos {min_candles + 1} velas, hay {len(ohlcv)}.")
        return

    executor = PaperOrderExecutor(
        backtest_paper_cfg,
        backtest_core_cfg,
        settings.logging.log_dir,
        strategy.name,
        execution_mode="backtest",
    )
    sizer = PositionSizer(settings.risk)
    first_ts = ohlcv.iloc[min_candles]["timestamp"] / 1000
    first_candle_date = datetime.fromtimestamp(first_ts, tz=UTC).date()
    circuit_breaker = DailyLossCircuitBreaker(
        settings.risk, executor.get_balance(), as_of_date=first_candle_date
    )

    symbol = settings.exchange.symbol
    opened, closed, skipped_breaker, skipped_balance = 0, 0, 0, 0

    # Ventana acotada, igual que en producción (BotEngine pide fetch_ohlcv con
    # límite fijo, no todo el historial): evita que evaluate() recalcule EMA/RSI/ATR
    # sobre una ventana creciente sin límite, que sería O(n²) en el total de velas.
    window_size = max(min_candles * 3, 200)

    for i in range(min_candles, len(ohlcv)):
        window_start = max(0, i + 1 - window_size)
        window = ohlcv.iloc[window_start : i + 1]
        candle = ohlcv.iloc[i]
        current_price = float(candle["close"])

        candle_datetime = datetime.fromtimestamp(candle["timestamp"] / 1000, tz=UTC)

        balance = executor.get_balance()
        circuit_breaker.register_balance(balance, as_of_date=candle_datetime.date())

        result = executor.poll_closed_trade(
            symbol,
            current_price_hint=current_price,
            high_hint=float(candle["high"]),
            low_hint=float(candle["low"]),
            timestamp_hint=candle_datetime,
        )
        if result is not None:
            closed += 1

        executor.update_trailing_stop(symbol, window, settings.strategy.trailing_stop_channel_period)

        strategy_result = strategy.evaluate(window)
        if strategy_result.signal in (Signal.HOLD, Signal.CLOSE):
            continue

        if not circuit_breaker.can_open_position():
            skipped_breaker += 1
            continue

        open_positions = executor.get_open_positions()
        if not validate_max_open_positions(len(open_positions), settings.risk):
            continue

        if strategy_result.atr is None:
            continue

        balance = executor.get_balance()
        sizing = sizer.calculate(
            balance=balance,
            entry_price=current_price,
            atr=strategy_result.atr,
            atr_sl_multiplier=settings.strategy.atr_sl_multiplier,
            atr_tp_multiplier=settings.strategy.atr_tp_multiplier,
            is_long=strategy_result.signal is Signal.LONG,
        )

        required_margin = (sizing.amount * current_price) / max(settings.exchange.leverage, 1)
        try:
            validate_sufficient_balance(balance, required_margin)
        except Exception:  # noqa: BLE001 - sin balance suficiente, se omite esta señal y se sigue
            skipped_balance += 1
            continue

        executor.open_position(
            symbol=symbol,
            signal=strategy_result.signal,
            amount=sizing.amount,
            entry_price_hint=current_price,
            stop_loss_price=sizing.stop_loss_price,
            take_profit_price=sizing.take_profit_price,
            signal_reason=strategy_result.reason,
            indicators=strategy_result.indicators,
            timestamp_hint=candle_datetime,
        )
        opened += 1

    print("=" * 60)
    print("BACKTEST COMPLETO")
    print("=" * 60)
    print(f"  Velas procesadas:          {len(ohlcv) - min_candles}")
    print(f"  Posiciones abiertas:       {opened}")
    print(f"  Posiciones cerradas:       {closed}")
    print(f"  Omitidas (circuit breaker):{skipped_breaker:>4}")
    print(f"  Omitidas (saldo insuf.):   {skipped_balance}")
    print(f"  Balance final simulado:    {executor.get_balance():,.2f} USDT")
    print()
    print("Ver el detalle con:")
    print("  python scripts/analyze_performance.py --mode backtest")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest de la estrategia activa contra velas históricas de BingX."
    )
    parser.add_argument("--days", type=int, default=90, help="Días de historial a descargar (default: 90).")
    parser.add_argument(
        "--timeframe", default=None, help="Timeframe a usar (default: el de .env / TIMEFRAME)."
    )
    parser.add_argument(
        "--reset", action="store_true", help="Descarta el balance simulado previo del backtest."
    )
    args = parser.parse_args()
    run_backtest(args.days, args.timeframe, args.reset)


if __name__ == "__main__":
    main()
