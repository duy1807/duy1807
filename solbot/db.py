"""SQLite persistence (fresh schema, WAL mode).

All amounts in lamports are INTEGER; token raw amounts and prices are TEXT
(u64 can overflow SQLite's signed 64-bit INTEGER; prices are exact Decimals).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL
);

CREATE TABLE IF NOT EXISTS wallets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, pubkey TEXT UNIQUE NOT NULL, label TEXT, created_at REAL
);

CREATE TABLE IF NOT EXISTS tokens (
  mint TEXT PRIMARY KEY, symbol TEXT, name TEXT, decimals INTEGER, token_program TEXT,
  supply TEXT, updated_at REAL
);

CREATE TABLE IF NOT EXISTS pools (
  address TEXT PRIMARY KEY, mint TEXT NOT NULL, dex TEXT NOT NULL, base_vault TEXT, quote_vault TEXT,
  quote_mint TEXT, creator TEXT, coin_creator TEXT, is_pump_pool INTEGER, extra_json TEXT, updated_at REAL
);
CREATE INDEX IF NOT EXISTS ix_pools_mint ON pools(mint);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                 -- NOW | DIP | LIMIT
  mint TEXT NOT NULL,
  route TEXT NOT NULL,                -- pumpswap | jupiter
  pool TEXT,
  count INTEGER NOT NULL,             -- 1 | 3 | 5
  sol_per_buy_lamports INTEGER NOT NULL,
  slippage_bps INTEGER NOT NULL,
  priority_micro_lamports INTEGER NOT NULL,
  cu_limit INTEGER NOT NULL,
  tip_lamports INTEGER NOT NULL,
  dip_pct TEXT, reference_price TEXT, trigger_price TEXT,
  tp_pct TEXT, sl_pct TEXT,
  exec_mode TEXT NOT NULL,            -- NO_SEND | LIVE, fixed at creation
  status TEXT NOT NULL,
  status_reason TEXT,
  submission_id INTEGER,
  wait_note TEXT,
  infra_rearms INTEGER NOT NULL DEFAULT 0,
  chat_id INTEGER,
  created_at REAL, armed_at REAL, triggered_at REAL, finished_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS ix_orders_mint ON orders(mint);

CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  purpose TEXT NOT NULL,              -- BUY | SELL
  order_id INTEGER, sell_id INTEGER,
  mint TEXT NOT NULL, route TEXT NOT NULL, count INTEGER NOT NULL,
  exec_mode TEXT NOT NULL,
  status TEXT NOT NULL,
  bundle_id TEXT,
  blockhash TEXT, last_valid_block_height INTEGER,
  snapshot_slot INTEGER, snapshot_age_ms REAL,
  tip_lamports INTEGER, tip_account TEXT, priority_micro_lamports INTEGER, cu_limit INTEGER,
  sim_method TEXT, sim_ok INTEGER, sim_detail TEXT,
  jito_endpoint TEXT, ack_detail TEXT,
  t_signal REAL, t_planned REAL, t_signed REAL, t_simulated REAL, t_sent REAL, t_ack REAL,
  t_landed REAL, t_verified REAL,
  landed_slot INTEGER, verify_level INTEGER, error TEXT,
  created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS ix_sub_status ON submissions(status);

CREATE TABLE IF NOT EXISTS sub_txs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  submission_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  signature TEXT UNIQUE NOT NULL,
  raw_b64 TEXT,
  status TEXT NOT NULL,
  planned_in_lamports INTEGER, planned_out_raw TEXT,
  max_in_lamports INTEGER, min_out_raw TEXT,
  limit_price TEXT, planned_price TEXT, has_tip INTEGER,
  actual_sol_delta INTEGER, actual_token_delta TEXT, fee_lamports INTEGER, tip_paid_lamports INTEGER,
  rent_lamports INTEGER, swap_lamports INTEGER, exec_price TEXT,
  slot INTEGER, err TEXT, receipt_json TEXT, verify_a TEXT, verify_b TEXT,
  updated_at REAL
);
CREATE INDEX IF NOT EXISTS ix_subtx_sub ON sub_txs(submission_id);

CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mint TEXT NOT NULL, wallet TEXT NOT NULL, status TEXT NOT NULL,   -- OPEN | CLOSED
  route TEXT, decimals INTEGER,
  token_raw TEXT NOT NULL, cost_lamports INTEGER NOT NULL,
  bought_raw_total TEXT NOT NULL, cost_total_lamports INTEGER NOT NULL,
  sold_raw_total TEXT NOT NULL, proceeds_total_lamports INTEGER NOT NULL,
  realized_pnl_lamports INTEGER NOT NULL, fees_lamports INTEGER NOT NULL,
  rent_lamports INTEGER NOT NULL DEFAULT 0,
  tp_pct TEXT, sl_pct TEXT, tpsl_enabled INTEGER NOT NULL DEFAULT 0,
  exit_state TEXT NOT NULL DEFAULT 'IDLE', exit_attempts INTEGER NOT NULL DEFAULT 0,
  opened_at REAL, closed_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS ix_pos_mint ON positions(mint, status);

CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL, side TEXT NOT NULL, submission_id INTEGER, sub_tx_id INTEGER UNIQUE,
  token_raw TEXT NOT NULL, sol_lamports INTEGER NOT NULL, fee_lamports INTEGER, tip_lamports INTEGER,
  swap_lamports INTEGER, price TEXT, created_at REAL
);

CREATE TABLE IF NOT EXISTS sells (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER, mint TEXT NOT NULL, route TEXT NOT NULL, pct TEXT, amount_raw TEXT,
  reason TEXT NOT NULL,               -- MANUAL | TP | SL
  exec_mode TEXT NOT NULL, status TEXT NOT NULL, status_reason TEXT,
  submission_id INTEGER, slippage_bps INTEGER, min_out_lamports INTEGER, expected_out_lamports INTEGER,
  proceeds_lamports INTEGER, cost_alloc_lamports INTEGER, pnl_lamports INTEGER,
  chat_id INTEGER, created_at REAL, finished_at REAL, updated_at REAL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, level TEXT NOT NULL,
  category TEXT NOT NULL, ref TEXT, message TEXT NOT NULL, data_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, name TEXT NOT NULL, value_ms REAL NOT NULL, ref TEXT
);
CREATE INDEX IF NOT EXISTS ix_metrics_name ON metrics(name, ts);
"""


class Database:
    """Small synchronous SQLite wrapper. Calls are sub-millisecond; WAL keeps readers non-blocking."""

    def __init__(self, path: str | Path, sync_full: bool = False):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(f"PRAGMA synchronous={'FULL' if sync_full else 'NORMAL'}")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            cur = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'")
            row = cur.fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
                )

    # -- generic helpers -------------------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            r = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(r) if r else None

    def insert(self, table: str, row: dict) -> int:
        cols = ",".join(row.keys())
        qs = ",".join("?" for _ in row)
        with self._lock:
            cur = self._conn.execute(f"INSERT INTO {table}({cols}) VALUES({qs})", tuple(row.values()))
            return int(cur.lastrowid)

    def update(self, table: str, row_id: int, fields: dict) -> None:
        if not fields:
            return
        if "updated_at" not in fields and table in _HAS_UPDATED_AT:
            fields = {**fields, "updated_at": time.time()}
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*fields.values(), row_id))

    def get(self, table: str, row_id: int) -> dict | None:
        return self.one(f"SELECT * FROM {table} WHERE id=?", (row_id,))

    def cas_status(self, table: str, row_id: int, from_statuses: Iterable[str], to_status: str,
                   extra: dict | None = None) -> bool:
        """Atomically move a row to ``to_status`` only if it is currently in ``from_statuses``.

        Used for one-shot claims (e.g. ARMED -> TRIGGERED): returns False if someone else won.
        """
        froms = list(from_statuses)
        fields = {"status": to_status, **(extra or {})}
        if table in _HAS_UPDATED_AT:
            fields.setdefault("updated_at", time.time())
        sets = ",".join(f"{k}=?" for k in fields)
        qs = ",".join("?" for _ in froms)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE {table} SET {sets} WHERE id=? AND status IN ({qs})",
                (*fields.values(), row_id, *froms),
            )
            return cur.rowcount == 1

    def transaction(self):
        return _Tx(self)

    # -- settings ---------------------------------------------------------------------------
    def get_setting(self, key: str) -> str | None:
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else None

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, time.time()),
        )

    # -- events / metrics -------------------------------------------------------------------
    def log_event(self, level: str, category: str, message: str, ref: str | None = None,
                  data: dict | None = None) -> None:
        self.insert(
            "events",
            {
                "ts": time.time(),
                "level": level,
                "category": category,
                "ref": ref,
                "message": message[:2000],
                "data_json": json.dumps(data, default=str)[:20000] if data else None,
            },
        )

    def metric(self, name: str, value_ms: float, ref: str | None = None) -> None:
        self.insert("metrics", {"ts": time.time(), "name": name, "value_ms": float(value_ms), "ref": ref})

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_HAS_UPDATED_AT = {"orders", "submissions", "sub_txs", "positions", "sells"}


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db._conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.db._conn.execute("COMMIT")
            else:
                self.db._conn.execute("ROLLBACK")
        finally:
            self.db._lock.release()
        return False
