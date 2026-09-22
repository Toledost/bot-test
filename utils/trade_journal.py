"""Registro de aprendizaje: guarda el contexto de cada decisión y su resultado.

A diferencia de PaperStore (estado operativo de la simulación: balance, fills),
el TradeJournal es un historial de solo-escritura pensado para análisis
posterior. Lo alimentan el modo paper, el modo live, y scripts/backtest.py
(execution_mode="backtest"), para que el aprendizaje de la estrategia no se
corte al pasar a capital real ni dependa de esperar señales en tiempo real.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_mode TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    amount REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    stop_loss_price REAL NOT NULL,
    take_profit_price REAL NOT NULL,
    signal_reason TEXT NOT NULL,
    indicators_json TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    pnl REAL,
    fee_paid REAL NOT NULL DEFAULT 0,
    close_reason TEXT,
    status TEXT NOT NULL DEFAULT 'open'
);
"""


class TradeJournal:
    """Acceso a la base de datos SQLite del historial de aprendizaje (data/journal.db)."""

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record_open(
        self,
        *,
        execution_mode: str,
        strategy_name: str,
        symbol: str,
        side: str,
        amount: float,
        entry_price: float,
        stop_loss_price: float,
        take_profit_price: float,
        signal_reason: str,
        indicators: dict[str, float],
        opened_at: str,
        fee_paid: float,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO journal_entries
                    (execution_mode, strategy_name, symbol, side, amount, entry_price,
                     stop_loss_price, take_profit_price, signal_reason, indicators_json,
                     opened_at, fee_paid, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (
                    execution_mode,
                    strategy_name,
                    symbol,
                    side,
                    amount,
                    entry_price,
                    stop_loss_price,
                    take_profit_price,
                    signal_reason,
                    json.dumps(indicators),
                    opened_at,
                    fee_paid,
                ),
            )
            assert cursor.lastrowid is not None  # garantizado por sqlite3 tras un INSERT exitoso
            return cursor.lastrowid

    def record_close(
        self,
        entry_id: int,
        *,
        closed_at: str,
        exit_price: float,
        pnl: float,
        extra_fee: float,
        close_reason: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE journal_entries
                SET closed_at = ?, exit_price = ?, pnl = ?, fee_paid = fee_paid + ?,
                    close_reason = ?, status = 'closed'
                WHERE id = ?
                """,
                (closed_at, exit_price, pnl, extra_fee, close_reason, entry_id),
            )

    def update_stop_loss(self, entry_id: int, new_stop_loss_price: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE journal_entries SET stop_loss_price = ? WHERE id = ?",
                (new_stop_loss_price, entry_id),
            )

    def delete_by_mode(self, execution_mode: str) -> int:
        """Elimina todas las entradas de un execution_mode dado (ej. limpiar backtests previos).

        Uso previsto: scripts/backtest.py --reset, para que corridas sucesivas
        con distinta configuración no se mezclen bajo el mismo execution_mode
        en el análisis. Nunca se llama con "paper" o "live" desde el bot.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM journal_entries WHERE execution_mode = ?", (execution_mode,)
            )
            return cursor.rowcount

    def get_open_entry(self, symbol: str, execution_mode: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_entries "
                "WHERE status = 'open' AND symbol = ? AND execution_mode = ? "
                "ORDER BY id DESC LIMIT 1",
                (symbol, execution_mode),
            ).fetchone()
            return cast(sqlite3.Row | None, row)
