"""Auto Sell TP/SL.

Activated only for positions created from *verified* BUY fills. On each fresh snapshot (<= 600 ms)
the position value is the exact sell quote of the whole position (price impact + pool fees included,
PumpSwap math) — Jupiter positions are re-quoted periodically. PnL% = (value - cost) / cost.
  * value >= cost * (1 + TP%)  -> SELL 100% (reason TP)
  * value <= cost * (1 - SL%)  -> SELL 100% (reason SL)
One exit at a time per position (exit_state PENDING). A provably failed exit (FAILED_SAFE) re-arms the
exit up to ``tpsl_max_exit_attempts``; an UNKNOWN/HOLD exit blocks further exits until reconciled.
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

from solders.pubkey import Pubkey

from .db import Database
from .engine import ORDER_FAILED, ORDER_FILLED, ORDER_HOLD, TradeEngine
from .market import ROUTE_JUPITER, MarketEngine, Snapshot
from .positions import PositionManager
from .settings import Settings
from . import constants as C

log = logging.getLogger("solbot.autosell")


class AutoSell:
    def __init__(self, db: Database, market: MarketEngine, engine: TradeEngine, positions: PositionManager,
                 settings: Settings, max_age_ms: int = 600):
        self.db = db
        self.market = market
        self.engine = engine
        self.positions = positions
        self.settings = settings
        self.max_age_ms = max_age_ms
        self._tasks: set[asyncio.Task] = set()
        self._jup_task: asyncio.Task | None = None
        market.add_listener(self.on_snapshot)
        engine.on_position_opened = self.on_position_opened
        engine.on_sell_final = self.on_sell_final

    async def restore(self) -> int:
        n = 0
        for p in self.positions.list_open():
            await self.market.watch(Pubkey.from_string(p["mint"]), f"pos:{p['id']}", p["route"] or "auto")
            n += 1
        return n

    async def on_position_opened(self, pos: dict) -> None:
        await self.market.watch(Pubkey.from_string(pos["mint"]), f"pos:{pos['id']}", pos["route"] or "auto")

    async def on_sell_final(self, sell: dict) -> None:
        pid = sell.get("position_id")
        if not pid:
            return
        pos = self.positions.get(pid)
        if pos is None:
            return
        if pos["status"] == "CLOSED":
            self.market.unwatch(Pubkey.from_string(pos["mint"]), f"pos:{pid}")
            return
        if sell["reason"] not in ("TP", "SL"):
            return
        if sell["status"] == ORDER_FILLED:
            self.db.update("positions", pid, {"exit_state": "IDLE"})
        elif sell["status"] == ORDER_FAILED:
            attempts = pos["exit_attempts"]
            if attempts >= self.settings.int("tpsl_max_exit_attempts"):
                self.db.update("positions", pid, {"exit_state": "IDLE", "tpsl_enabled": 0})
                self.engine.notify(sell["chat_id"], f"⚠️ TP/SL vị thế #{pid} tắt sau {attempts} lần SELL thất bại.")
            else:
                self.db.update("positions", pid, {"exit_state": "IDLE"})
        elif sell["status"] == ORDER_HOLD:
            self.db.update("positions", pid, {"exit_state": "HOLD"})

    def evaluate(self, pos: dict, value_lamports: int) -> str | None:
        cost = int(pos["cost_lamports"])
        if cost <= 0:
            return None
        pnl = Decimal(value_lamports - cost) / Decimal(cost) * 100
        tp = Decimal(pos["tp_pct"]) if pos["tp_pct"] else None
        sl = Decimal(pos["sl_pct"]) if pos["sl_pct"] else None
        if tp is not None and tp > 0 and pnl >= tp:
            return "TP"
        if sl is not None and sl > 0 and pnl <= -sl:
            return "SL"
        return None

    def _candidates(self, mint: str) -> list[dict]:
        return self.db.query(
            "SELECT * FROM positions WHERE mint=? AND status='OPEN' AND tpsl_enabled=1 AND exit_state='IDLE'",
            (mint,))

    def on_snapshot(self, snap: Snapshot) -> None:
        if self.settings.paused or snap.age_ms() > self.max_age_ms or snap.ps_state is None:
            return
        for pos in self._candidates(str(snap.mint)):
            tokens = int(pos["token_raw"])
            if tokens <= 0:
                continue
            try:
                value = snap.ps_state.quote_sell(tokens).user_out
            except Exception:
                continue
            reason = self.evaluate(pos, value)
            if reason:
                self._fire(pos, reason, value)

    def _fire(self, pos: dict, reason: str, value: int) -> None:
        if not self.db.execute(
                "UPDATE positions SET exit_state='PENDING', exit_attempts=exit_attempts+1, updated_at=? "
                "WHERE id=? AND exit_state='IDLE' AND tpsl_enabled=1", (time.time(), pos["id"])).rowcount:
            return
        chat = self.db.one("SELECT chat_id FROM orders WHERE mint=? AND chat_id IS NOT NULL ORDER BY id DESC LIMIT 1",
                           (pos["mint"],))
        chat_id = chat["chat_id"] if chat else None
        self.db.log_event("INFO", "tpsl", f"{reason} position #{pos['id']} value={value}", ref=f"pos:{pos['id']}")

        async def run():
            res = await self.engine.execute_sell(pos["mint"], Decimal(100), reason, chat_id)
            if res.status in ("PRESEND_FAILED", "DRY_RUN"):
                p = self.positions.get(pos["id"])
                if res.status == "DRY_RUN":
                    # NO_SEND: record the would-be exit and stop auto-selling this position
                    self.db.update("positions", pos["id"], {"exit_state": "IDLE", "tpsl_enabled": 0})
                    self.engine.notify(chat_id, f"🧪 NO_SEND: {reason} vị thế #{pos['id']} sẽ SELL 100% (không gửi).")
                elif p and p["exit_attempts"] >= self.settings.int("tpsl_max_exit_attempts"):
                    self.db.update("positions", pos["id"], {"exit_state": "IDLE", "tpsl_enabled": 0})
                else:
                    self.db.update("positions", pos["id"], {"exit_state": "IDLE"})

        t = asyncio.create_task(run())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # Jupiter-routed positions: periodic exact sell quote of the whole position
    def start(self, interval_ms: int = 1500) -> None:
        async def loop():
            while True:
                await asyncio.sleep(interval_ms / 1000)
                if self.settings.paused or self.engine.jupiter is None:
                    continue
                for pos in self.db.query("SELECT * FROM positions WHERE status='OPEN' AND tpsl_enabled=1 "
                                         "AND exit_state='IDLE' AND route=?", (ROUTE_JUPITER,)):
                    try:
                        q = await self.engine.jupiter.quote(Pubkey.from_string(pos["mint"]), C.WSOL_MINT,
                                                            int(pos["token_raw"]), 100)
                    except Exception:
                        continue
                    reason = self.evaluate(pos, q.out_amount)
                    if reason:
                        self._fire(pos, reason, q.out_amount)

        self._jup_task = asyncio.create_task(loop(), name="autosell-jup")

    async def stop(self) -> None:
        if self._jup_task:
            self._jup_task.cancel()
