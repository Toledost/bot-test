"""Persistencia SQLite para el estado y el historial de paper trading."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    amount REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    stop_loss_price REAL NOT NULL,
    take_profit_price REAL NOT NULL,
    fee_paid REAL NOT NULL DEFAULT 0,
    pnl REAL,
    status TEXT NOT NULL DEFAULT 'open'
);
"""


class PaperStore:
    """Acceso a la base de datos SQLite que respalda el paper trading."""

    def __init__(self, db_path: str, initial_balance: float) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._init_schema(initial_balance)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self, initial_balance: float) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT balance FROM account_state WHERE id = 1").fetchone()
            if row is None:
                conn.execute("INSERT INTO account_state (id, balance) VALUES (1, ?)", (initial_balance,))

    def get_balance(self) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT balance FROM account_state WHERE id = 1").fetchone()
            return float(row["balance"])

    def set_balance(self, balance: float) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE account_state SET balance = ? WHERE id = 1", (balance,))

    def insert_open_trade(
        self,
        *,
        opened_at: str,
        symbol: str,
        side: str,
        amount: float,
        entry_price: float,
        stop_loss_price: float,
        take_profit_price: float,
        fee_paid: float,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO trades
                    (opened_at, symbol, side, amount, entry_price, stop_loss_price,
                     take_profit_price, fee_paid, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (opened_at, symbol, side, amount, entry_price, stop_loss_price, take_profit_price, fee_paid),
            )
            assert cursor.lastrowid is not None  # garantizado por sqlite3 tras un INSERT exitoso
            return cursor.lastrowid

    def close_trade(
        self, trade_id: int, *, closed_at: str, exit_price: float, pnl: float, extra_fee: float
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trades
                SET closed_at = ?, exit_price = ?, pnl = ?, fee_paid = fee_paid + ?, status = 'closed'
                WHERE id = ?
                """,
                (closed_at, exit_price, pnl, extra_fee, trade_id),
            )

    def get_open_trades(self, symbol: str | None = None) -> list[sqlite3.Row]:
        with self._connect() as conn:
            if symbol:
                return conn.execute(
                    "SELECT * FROM trades WHERE status = 'open' AND symbol = ?", (symbol,)
                ).fetchall()
            return conn.execute("SELECT * FROM trades WHERE status = 'open'").fetchall()
