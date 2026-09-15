"""Analiza el journal de trades (data/journal.db) para guiar ajustes de estrategia.

A diferencia de monitor_paper.py (snapshot operativo del estado actual), este
script segmenta el historial completo —paper y/o live— por las condiciones de
mercado presentes al momento de cada entrada (RSI, ATR, lado LONG/SHORT) para
mostrar qué contextos produjeron mejores o peores resultados. Es una
herramienta de lectura humana: el bot no ajusta parámetros solo a partir de
esto, la decisión de cambiar .env queda en manos del operador.

Uso:
    python scripts/analyze_performance.py
    python scripts/analyze_performance.py --mode paper
    python scripts/analyze_performance.py --mode live
    python scripts/analyze_performance.py --mode backtest
    python scripts/analyze_performance.py --symbol BTC/USDT:USDT
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import load_settings  # noqa: E402

_DIVIDER = "-" * 78


def _resolve_db_path() -> str:
    settings = load_settings()
    db_path = settings.core.journal_db_path

    if Path(db_path).exists():
        return db_path

    if db_path.startswith("/data/"):
        local_candidate = Path(__file__).resolve().parent.parent / "data" / Path(db_path).name
        if local_candidate.exists():
            return str(local_candidate)

    return db_path


def _connect_readonly(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        print(f"No se encontró el journal en: {db_path}")
        print("¿El bot ya cerró al menos un trade en paper o live?")
        sys.exit(1)

    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_closed_trades(
    conn: sqlite3.Connection, mode: str | None, symbol: str | None
) -> list[dict]:
    query = "SELECT * FROM journal_entries WHERE status = 'closed'"
    params: list[str] = []
    if mode:
        query += " AND execution_mode = ?"
        params.append(mode)
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    query += " ORDER BY id ASC"

    rows = conn.execute(query, params).fetchall()
    trades = []
    for row in rows:
        trade = dict(row)
        try:
            trade["indicators"] = json.loads(trade["indicators_json"])
        except (TypeError, ValueError):
            trade["indicators"] = {}
        trades.append(trade)
    return trades


def _win_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if (t["pnl"] or 0) > 0)
    return wins / len(trades) * 100


def _avg_pnl(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    return sum(t["pnl"] or 0 for t in trades) / len(trades)


def _print_overview(trades: list[dict]) -> None:
    total_pnl = sum(t["pnl"] or 0 for t in trades)
    print(_DIVIDER)
    print("RESUMEN GENERAL")
    print(_DIVIDER)
    print(f"  Trades cerrados:     {len(trades)}")
    print(f"  Win rate global:     {_win_rate(trades):.1f}%")
    print(f"  PnL total:           {total_pnl:+,.2f}")
    print(f"  PnL promedio/trade:  {_avg_pnl(trades):+,.4f}")
    print()


def _print_by_side(trades: list[dict]) -> None:
    print(_DIVIDER)
    print("POR LADO (LONG vs SHORT)")
    print(_DIVIDER)
    for side, label in (("buy", "LONG"), ("sell", "SHORT")):
        subset = [t for t in trades if t["side"] == side]
        if not subset:
            continue
        print(
            f"  {label:<6} n={len(subset):>4}  win_rate={_win_rate(subset):>5.1f}%  "
            f"pnl_total={sum(t['pnl'] or 0 for t in subset):>+10.2f}  "
            f"pnl_avg={_avg_pnl(subset):>+8.4f}"
        )
    print()


def _print_by_close_reason(trades: list[dict]) -> None:
    print(_DIVIDER)
    print("POR MOTIVO DE CIERRE")
    print(_DIVIDER)
    for reason in ("take_profit", "stop_loss", "manual"):
        subset = [t for t in trades if t["close_reason"] == reason]
        if not subset:
            continue
        print(f"  {reason:<12} n={len(subset):>4}  pnl_total={sum(t['pnl'] or 0 for t in subset):>+10.2f}")
    print()


def _print_by_indicator_bucket(
    trades: list[dict], indicator: str, buckets: list[tuple[float, float, str]]
) -> None:
    """Segmenta trades por rangos de un indicador capturado al momento de la entrada."""
    relevant = [t for t in trades if indicator in t["indicators"]]
    if not relevant:
        return

    print(_DIVIDER)
    print(f"POR {indicator.upper()} AL MOMENTO DE ENTRAR")
    print(_DIVIDER)
    for low, high, label in buckets:
        subset = [t for t in relevant if low <= t["indicators"][indicator] < high]
        if not subset:
            continue
        print(
            f"  {label:<12} n={len(subset):>4}  win_rate={_win_rate(subset):>5.1f}%  "
            f"pnl_total={sum(t['pnl'] or 0 for t in subset):>+10.2f}  "
            f"pnl_avg={_avg_pnl(subset):>+8.4f}"
        )
    print()


def _print_atr_bucket(trades: list[dict]) -> None:
    """Segmenta por distancia al SL relativa al precio de entrada.

    El ATR crudo no viaja en `indicators` (es un campo separado de
    StrategyResult, no capturado en el journal); se aproxima aquí usando
    stop_loss_price/entry_price como proxy de la volatilidad al momento de
    abrir, ya que el SL se deriva directamente del ATR en risk_management.
    """
    relevant = [
        {**t, "_atr_pct": abs(t["entry_price"] - t["stop_loss_price"]) / t["entry_price"] * 100}
        for t in trades
        if t["entry_price"]
    ]
    if not relevant:
        return

    buckets = [(0.0, 0.5, "<0.5%"), (0.5, 1.5, "0.5-1.5%"), (1.5, 3.0, "1.5-3%"), (3.0, 999.0, ">3%")]
    print(_DIVIDER)
    print("POR DISTANCIA AL STOP LOSS (proxy de volatilidad en la entrada)")
    print(_DIVIDER)
    for low, high, label in buckets:
        subset = [t for t in relevant if low <= t["_atr_pct"] < high]
        if not subset:
            continue
        print(
            f"  {label:<12} n={len(subset):>4}  win_rate={_win_rate(subset):>5.1f}%  "
            f"pnl_total={sum(t['pnl'] or 0 for t in subset):>+10.2f}"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Analiza performance histórica del journal de trades.")
    parser.add_argument(
        "--mode", choices=["paper", "live", "backtest"], default=None, help="Filtrar por modo de ejecución."
    )
    parser.add_argument("--symbol", default=None, help="Filtrar por símbolo (ej. BTC/USDT:USDT).")
    args = parser.parse_args()

    db_path = _resolve_db_path()
    conn = _connect_readonly(db_path)
    try:
        trades = _load_closed_trades(conn, args.mode, args.symbol)
    finally:
        conn.close()

    if not trades:
        print("Sin trades cerrados todavía para el filtro solicitado.")
        return

    _print_overview(trades)
    _print_by_side(trades)
    _print_by_close_reason(trades)
    _print_by_indicator_bucket(
        trades,
        "rsi",
        [(0.0, 30.0, "<30 (oversold)"), (30.0, 50.0, "30-50"), (50.0, 70.0, "50-70"), (70.0, 100.0, ">=70")],
    )
    _print_atr_bucket(trades)

    print(_DIVIDER)
    print(
        "Nota: estas métricas son orientativas. Con pocos trades (n < ~30 por\n"
        "segmento) las diferencias pueden deberse al azar, no a una ventaja real\n"
        "de esa condición. No ajustes parámetros de estrategia basándote en\n"
        "muestras chicas."
    )
    print(_DIVIDER)


if __name__ == "__main__":
    main()
