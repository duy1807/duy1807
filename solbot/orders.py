"""Order service: creates BUY NGAY / FIXED DIP / LIMIT orders (×1/×3/×5) with locked parameters."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal

from solders.pubkey import Pubkey

from .db import Database
from .engine import ORDER_ARMED, ORDER_TRIGGERED, ExecResult, TradeEngine
from .market import ROUTE_PUMPSWAP, MarketEngine
from .settings import Settings
from .triggers import TriggerManager

KINDS = ("NOW", "DIP", "LIMIT")


class OrderError(ValueError):
    pass


@dataclass
class OrderParams:
    sol_per_buy: Decimal
    count: int
    slippage_pct: Decimal
    tp_pct: Decimal
    sl_pct: Decimal
    dip_pct: Decimal | None = None
    limit_price: Decimal | None = None


class OrderService:
    def __init__(self, db: Database, settings: Settings, market: MarketEngine, engine: TradeEngine,
                 triggers: TriggerManager):
        self.db = db
        self.settings = settings
        self.market = market
        self.engine = engine
        self.triggers = triggers
        self._tasks: dict[int, asyncio.Task] = {}

    def default_params(self) -> OrderParams:
        s = self.settings
        return OrderParams(sol_per_buy=s.dec("sol_per_buy"), count=s.int("buy_count"),
                           slippage_pct=s.dec("slippage_pct"), tp_pct=s.dec("tp_pct"), sl_pct=s.dec("sl_pct"),
                           dip_pct=s.dec("dip_pct"))

    def _validate(self, kind: str, p: OrderParams) -> None:
        if kind not in KINDS:
            raise OrderError("loại lệnh không hợp lệ")
        if p.count not in (1, 3, 5):
            raise OrderError("Số giao dịch phải là ×1, ×3 hoặc ×5")
        for key, val in (("sol_per_buy", p.sol_per_buy), ("slippage_pct", p.slippage_pct), ("tp_pct", p.tp_pct),
                         ("sl_pct", p.sl_pct)):
            self.settings.validate(key, str(val))
        if kind == "DIP":
            if p.dip_pct is None:
                raise OrderError("Thiếu DIP%")
            self.settings.validate("dip_pct", str(p.dip_pct))
        if kind == "LIMIT":
            if p.limit_price is None or p.limit_price <= 0:
                raise OrderError("Giá LIMIT phải > 0")

    async def create(self, kind: str, mint_s: str, p: OrderParams, chat_id: int | None) -> dict:
        self._validate(kind, p)
        mint = Pubkey.from_string(mint_s)
        m = await self.market.resolve(mint, self.settings.raw("route"))
        s = self.settings
        cu = s.int("cu_limit_pumpswap" if m.route == ROUTE_PUMPSWAP else "cu_limit_jupiter")
        row = {
            "kind": kind, "mint": mint_s, "route": m.route, "pool": str(m.pool.address) if m.pool else None,
            "count": p.count, "sol_per_buy_lamports": s.sol_to_lamports(p.sol_per_buy),
            "slippage_bps": int((p.slippage_pct * 100).to_integral_value()),
            "priority_micro_lamports": s.int("priority_micro_lamports"), "cu_limit": cu,
            "tip_lamports": s.tip_lamports(),
            "tp_pct": str(p.tp_pct) if p.tp_pct > 0 else None, "sl_pct": str(p.sl_pct) if p.sl_pct > 0 else None,
            "exec_mode": s.mode, "chat_id": chat_id, "created_at": time.time(), "updated_at": time.time(),
        }
        if kind == "NOW":
            row["status"] = ORDER_TRIGGERED
            row["triggered_at"] = time.time()
        else:
            snap = await self.market.fresh_snapshot(mint)
            if kind == "DIP":
                ref = snap.price_sol
                row["dip_pct"] = str(p.dip_pct)
                row["reference_price"] = str(ref)
                row["trigger_price"] = str((ref * (Decimal(1) - p.dip_pct / Decimal(100))).normalize())
            else:
                row["reference_price"] = str(snap.price_sol)
                row["trigger_price"] = str(p.limit_price.normalize())
            row["status"] = ORDER_ARMED
            row["armed_at"] = time.time()
        oid = self.db.insert("orders", row)
        self.db.log_event("INFO", "order", f"{kind} ×{p.count} created", ref=f"order:{oid}", data=row)
        if kind == "NOW":
            signal_ms = time.time() * 1000
            t = asyncio.create_task(self.engine.execute_buy(oid, signal_ms))
            self._tasks[oid] = t
            t.add_done_callback(lambda _t, oid=oid: self._tasks.pop(oid, None))
        else:
            await self.triggers.arm(oid)
        return self.db.get("orders", oid)

    async def create_and_wait(self, kind: str, mint_s: str, p: OrderParams, chat_id: int | None
                              ) -> tuple[dict, ExecResult | None]:
        """BUY NGAY helper for tests/CLI: returns the engine result once sent (or dry-run)."""
        order = await self.create(kind, mint_s, p, chat_id)
        if kind != "NOW":
            return order, None
        t = self._tasks.get(order["id"])
        res = await t if t is not None else None
        return self.db.get("orders", order["id"]), res

    def cancel(self, order_id: int) -> bool:
        return self.triggers.cancel(order_id)
