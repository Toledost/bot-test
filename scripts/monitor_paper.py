"""Monitor de solo lectura del estado de paper trading (balance, PnL, trades).

Lee directamente data/trades_paper.db sin interferir con el bot en ejecución.
Uso:
    python scripts/monitor_paper.py                 # snapshot único
    python scripts/monitor_paper.py --watch          # refresca cada 10s
    python scripts/monitor_paper.py --watch --interval 5
    python scripts/monitor_paper.py --last 20        # mostrar más trades
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

# Permite importar config/ tanto si se corre desde el host como dentro del contenedor.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import load_settings  # noqa: E402

_DIVIDER = "-" * 78


def _resolve_db_path() -> str:
    """Resuelve la ruta a la base de datos de paper trading.

    Dentro del contenedor, PAPER_DB_PATH apunta a /data/trades_paper.db (el
    volumen montado). Corriendo desde el host contra ese mismo volumen local
    (./data), se remapea /data -> ./data si el path absoluto no existe.
    """
    settings = load_settings()
    db_path = settings.paper.db_path

    if Path(db_path).exists():
        return db_path

    if db_path.startswith("/data/"):
        local_candidate = Path(__file__).resolve().parent.parent / "data" / Path(db_path).name
        if local_candidate.exists():
            return str(local_candidate)

    return db_path


def _connect_readonly(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        print(f"No se encontró la base de datos de paper trading en: {db_path}")
        print("¿El bot ya corrió al menos un ciclo en modo paper?")
        sys.exit(1)

    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:,.2f}"


def _print_snapshot(conn: sqlite3.Connection, last_n: int) -> None:
    balance_row = conn.execute("SELECT balance FROM account_state WHERE id = 1").fetchone()
    balance = float(balance_row["balance"]) if balance_row else 0.0

    closed = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(pnl), 0) AS total_pnl, "
        "COALESCE(SUM(fee_paid), 0) AS total_fees "
        "FROM trades WHERE status = 'closed'"
    ).fetchone()
    wins = conn.execute("SELECT COUNT(*) AS n FROM trades WHERE status = 'closed' AND pnl > 0").fetchone()
    open_trades = conn.execute("SELECT COUNT(*) AS n FROM trades WHERE status = 'open'").fetchone()

    total_closed = closed["n"] or 0
    win_rate = (wins["n"] / total_closed * 100) if total_closed else 0.0

    os.system("cls" if os.name == "nt" else "clear") if _WATCH_MODE else None

    print(_DIVIDER)
    print(f"BingX Trading Bot — Paper Trading Monitor  |  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(_DIVIDER)
    print(f"  Balance actual:        {balance:,.2f} USDT")
    print(f"  Posiciones abiertas:   {open_trades['n']}")
    print(f"  Trades cerrados:       {total_closed}")
    print(f"  Win rate:              {win_rate:.1f}%")
    print(f"  PnL acumulado:         {_fmt_money(closed['total_pnl'])} USDT")
    print(f"  Comisiones pagadas:    {closed['total_fees']:.2f} USDT")
    print(_DIVIDER)

    print(f"\nÚltimos {last_n} trades:\n")
    rows = conn.execute(
        "SELECT id, opened_at, closed_at, symbol, side, amount, entry_price, exit_price, pnl, status "
        "FROM trades ORDER BY id DESC LIMIT ?",
        (last_n,),
    ).fetchall()

    if not rows:
        print("  (sin trades registrados todavía)")
        return

    header = (
        f"  {'ID':>4}  {'Estado':<7} {'Side':<5} {'Símbolo':<16} "
        f"{'Entrada':>10} {'Salida':>10} {'PnL':>10}  Abierto (UTC)"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in rows:
        exit_price = f"{row['exit_price']:.2f}" if row["exit_price"] is not None else "-"
        pnl = _fmt_money(row["pnl"]) if row["pnl"] is not None else "-"
        opened_at = row["opened_at"].replace("T", " ").split(".")[0] if row["opened_at"] else "-"
        print(
            f"  {row['id']:>4}  {row['status']:<7} {row['side']:<5} {row['symbol']:<16} "
            f"{row['entry_price']:>10.2f} {exit_price:>10} {pnl:>10}  {opened_at}"
        )
    print()


_WATCH_MODE = False


def main() -> None:
    global _WATCH_MODE

    parser = argparse.ArgumentParser(description="Monitor de paper trading (solo lectura).")
    parser.add_argument(
        "--watch", action="store_true", help="Refresca periódicamente en lugar de un snapshot único."
    )
    parser.add_argument(
        "--interval", type=float, default=10.0, help="Segundos entre refrescos con --watch (default: 10)."
    )
    parser.add_argument(
        "--last", type=int, default=10, help="Cantidad de trades recientes a mostrar (default: 10)."
    )
    args = parser.parse_args()

    _WATCH_MODE = args.watch
    db_path = _resolve_db_path()

    try:
        if args.watch:
            while True:
                conn = _connect_readonly(db_path)
                try:
                    _print_snapshot(conn, args.last)
                finally:
                    conn.close()
                print(f"(actualizando cada {args.interval:.0f}s — Ctrl+C para salir)")
                time.sleep(args.interval)
        else:
            conn = _connect_readonly(db_path)
            try:
                _print_snapshot(conn, args.last)
            finally:
                conn.close()
    except KeyboardInterrupt:
        print("\nMonitor detenido.")


if __name__ == "__main__":
    main()
