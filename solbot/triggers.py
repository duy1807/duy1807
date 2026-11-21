"""FIXED DIP / LIMIT trigger manager.

* DIP:   reference price is locked at ARM time; trigger = reference * (1 - DIP%/100) (fixed, no trailing).
* LIMIT: trigger = the user's price (fixed, never raised).
Both fire on any real-time snapshot (no candles) whose spot price <= trigger, but only if the snapshot
is <= 600 ms old and every leg of the ×1/×3/×5 plan can be executed at or below the trigger price.
The ARMED -> TRIGGERED claim is an atomic compare-and-set, so an order can only ever be activated
once; after anything is sent the order never re-arms (verification decides its fate).
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

from solders.pubkey import Pubkey

from .db import Database
from .engine import ORDER_ARMED, ORDER_TRIGGERED, PriceCapNotMet, TradeEngine
from .market import MarketEngine, Snapshot
from .settings import MODE_LIVE, Settings

log = logging.getLogger("solbot.triggers")


class TriggerManager:
    def __init__(self, db: Database, market: MarketEngine, engine: TradeEngine, settings: Settings,
                 max_age_ms: int = 600):
        self.db = db
        self.market = market
        self.engine = engine
        self.settings = settings
        self.max_age_ms = max_age_ms
        self.armed: dict[str, dict[int, dict]] = {}  # mint -> order_id -> order row (cached)
        self.busy: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self._last_note: dict[int, float] = {}
        market.add_listener(self.on_snapshot)

    def start(self, interval_ms: int = 500) -> None:
        """Periodic re-evaluation on the current (heartbeat-confirmed) snapshot, so a quiet pool or a
        mode switch to LIVE does not leave a satisfied trigger waiting for the next trade."""

        async def loop():
            while True:
                await asyncio.sleep(interval_ms / 1000)
                for mint in list(self.armed):
                    snap = self.market.current(Pubkey.from_string(mint))
                    if snap is not None:
                        self.on_snapshot(snap)

        self._loop_task = asyncio.create_task(loop(), name="trigger-reeval")

    async def stop(self) -> None:
        t = getattr(self, "_loop_task", None)
        if t:
            t.cancel()

    async def restore(self) -> int:
        rows = self.db.query("SELECT * FROM orders WHERE status=? AND kind IN ('DIP','LIMIT')", (ORDER_ARMED,))
        for o in rows:
            await self._register(o)
        return len(rows)

    async def _register(self, order: dict) -> None:
        self.armed.setdefault(order["mint"], {})[order["id"]] = order
        await self.market.watch(Pubkey.from_string(order["mint"]), f"order:{order['id']}", order["route"])

    async def arm(self, order_id: int) -> dict:
        o = self.db.get("orders", order_id)
        if o is None or o["status"] != ORDER_ARMED:
            raise ValueError("order is not ARMED")
        await self._register(o)
        snap = self.market.current(Pubkey.from_string(o["mint"]))
        if snap is not None:
            self.on_snapshot(snap)
        return o

    def cancel(self, order_id: int) -> bool:
        o = self.db.get("orders", order_id)
        if o is None:
            return False
        ok = self.db.cas_status("orders", order_id, [ORDER_ARMED], "CANCELLED", {"finished_at": time.time()})
        if ok:
            self._unregister(o["mint"], order_id)
        return ok

    def _unregister(self, mint: str, order_id: int) -> None:
        d = self.armed.get(mint)
        if d:
            d.pop(order_id, None)
            if not d:
                self.armed.pop(mint, None)
        self.market.unwatch(Pubkey.from_string(mint), f"order:{order_id}")

    # -- hot path: called for every snapshot -------------------------------------------------
    def on_snapshot(self, snap: Snapshot) -> None:
        orders = self.armed.get(str(snap.mint))
        if not orders or self.settings.paused:
            return
        if snap.age_ms() > self.max_age_ms:
            return
        live = self.settings.is_live
        for oid, o in list(orders.items()):
            if oid in self.busy:
                continue
            if o["exec_mode"] == MODE_LIVE and not live:
                continue  # a LIVE order never fires while the bot is in NO_SEND
            if snap.price_sol <= Decimal(o["trigger_price"]):
                self.busy.add(oid)
                t = asyncio.create_task(self._fire(oid, snap, time.time() * 1000))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)

    async def _fire(self, order_id: int, snap: Snapshot, signal_ms: float) -> None:
        try:
            order = self.db.get("orders", order_id)
            if order is None or order["status"] != ORDER_ARMED:
                self._unregister(snap.mint.__str__(), order_id)
                return
            # cheap feasibility check before consuming the one-shot trigger
            try:
                await self.engine.plan_buy(order, snap)
            except PriceCapNotMet as e:
                self._note(order_id, str(e))
                return
            except Exception as e:
                self._note(order_id, f"plan: {e}")
                return
            if not self.db.cas_status("orders", order_id, [ORDER_ARMED], ORDER_TRIGGERED,
                                      {"triggered_at": time.time(), "wait_note": None}):
                return
            self.armed.get(order["mint"], {}).pop(order_id, None)
            self.db.log_event("INFO", "trigger", f"{order['kind']} #{order_id} triggered at {snap.price_sol}",
                              ref=f"order:{order_id}")
            res = await self.engine.execute_buy(order_id, signal_ms, snap)
            if res.status == "REARM":
                # nothing was sent: price moved back above the ceiling while re-preparing
                if self.db.cas_status("orders", order_id, [ORDER_TRIGGERED], ORDER_ARMED,
                                      {"wait_note": res.message[:300]}):
                    self.armed.setdefault(order["mint"], {})[order_id] = self.db.get("orders", order_id)
                    return
            self.market.unwatch(Pubkey.from_string(order["mint"]), f"order:{order_id}")
        finally:
            self.busy.discard(order_id)

    def _note(self, order_id: int, note: str) -> None:
        now = time.monotonic()
        if now - self._last_note.get(order_id, 0) > 2:
            self._last_note[order_id] = now
            self.db.update("orders", order_id, {"wait_note": note[:300]})
