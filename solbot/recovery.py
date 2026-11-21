"""Restart recovery and periodic reconciliation.

On start:
  * submissions that were PREPARED but never marked SENT were provably never handed to the network
    (SENT is persisted before the Jito call) -> FAILED_SAFE, and their order/sell is closed (no resend);
  * submissions SENT/ACK/PENDING/LANDED/UNKNOWN_HOLD -> verification resumes;
  * orders TRIGGERED without a submission -> FAILED_SAFE (interrupted before send; never re-sent);
  * ARMED DIP/LIMIT orders are re-registered with their locked reference/trigger prices;
  * open positions resume TP/SL monitoring; exits left PENDING without an open sell are reset.
Then a background loop keeps reconciling HOLD submissions with both verification RPCs.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .db import Database
from .engine import ORDER_FAILED, ORDER_TRIGGERED
from .verify import OPEN_SUB, ST_FAILED_SAFE, ST_PREPARED, Verifier

log = logging.getLogger("solbot.recovery")


class Recovery:
    def __init__(self, db: Database, verifier: Verifier, interval_ms: int = 3000, deadline_s: float = 150.0):
        self.db = db
        self.verifier = verifier
        self.interval_ms = interval_ms
        self.deadline_s = deadline_s
        self._task: asyncio.Task | None = None
        self._spawned: set[asyncio.Task] = set()

    def recover_db(self) -> dict:
        report = {"never_sent": 0, "resume_verify": [], "orders_interrupted": 0, "sells_interrupted": 0,
                  "exits_reset": 0}
        now = time.time()
        for s in self.db.query("SELECT * FROM submissions WHERE status=?", (ST_PREPARED,)):
            self.db.update("submissions", s["id"], {"status": ST_FAILED_SAFE,
                                                    "error": "interrupted before send (never sent)"})
            self.db.execute("UPDATE sub_txs SET status=?, err=? WHERE submission_id=?",
                            (ST_FAILED_SAFE, "never sent", s["id"]))
            if s["order_id"]:
                self.db.cas_status("orders", s["order_id"], [ORDER_TRIGGERED], ORDER_FAILED,
                                   {"status_reason": "interrupted before send", "finished_at": now})
            if s["sell_id"]:
                self.db.cas_status("sells", s["sell_id"], [ORDER_TRIGGERED], ORDER_FAILED,
                                   {"status_reason": "interrupted before send", "finished_at": now})
            report["never_sent"] += 1
        qs = ",".join("?" for _ in OPEN_SUB)
        for s in self.db.query(f"SELECT id FROM submissions WHERE status IN ({qs})", tuple(OPEN_SUB)):
            report["resume_verify"].append(s["id"])
        for o in self.db.query("SELECT * FROM orders WHERE status=? AND submission_id IS NULL", (ORDER_TRIGGERED,)):
            self.db.update("orders", o["id"], {"status": ORDER_FAILED, "finished_at": now,
                                               "status_reason": "interrupted before send (not re-sent)"})
            report["orders_interrupted"] += 1
        for o in self.db.query("SELECT * FROM sells WHERE status=? AND submission_id IS NULL", (ORDER_TRIGGERED,)):
            self.db.update("sells", o["id"], {"status": ORDER_FAILED, "finished_at": now,
                                              "status_reason": "interrupted before send (not re-sent)"})
            report["sells_interrupted"] += 1
        for p in self.db.query("SELECT * FROM positions WHERE status='OPEN' AND exit_state='PENDING'"):
            open_sell = self.db.one(
                f"SELECT s.id FROM sells s JOIN submissions u ON u.id=s.submission_id WHERE s.position_id=? "
                f"AND u.status IN ({qs})", (p["id"], *OPEN_SUB))
            if not open_sell:
                self.db.update("positions", p["id"], {"exit_state": "IDLE"})
                report["exits_reset"] += 1
        self.db.log_event("INFO", "recovery", "startup recovery", data=report)
        return report

    def resume(self, sub_ids: list[int]) -> None:
        for sid in sub_ids:
            t = asyncio.create_task(self.verifier.run(sid, deadline_s=self.deadline_s))
            self._spawned.add(t)
            t.add_done_callback(self._spawned.discard)

    def start(self) -> None:
        async def loop():
            while True:
                await asyncio.sleep(self.interval_ms / 1000)
                try:
                    qs = ",".join("?" for _ in OPEN_SUB)
                    for s in self.db.query(f"SELECT id FROM submissions WHERE status IN ({qs})", tuple(OPEN_SUB)):
                        if s["id"] not in self.verifier._active:
                            t = asyncio.create_task(self.verifier.run(s["id"], deadline_s=min(20.0, self.deadline_s)))
                            self._spawned.add(t)
                            t.add_done_callback(self._spawned.discard)
                except Exception:
                    log.exception("reconcile loop error")

        self._task = asyncio.create_task(loop(), name="reconcile")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
