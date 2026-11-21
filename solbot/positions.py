"""Position & PnL Manager (average-cost accounting, partial sells supported).

* cost basis  = SOL that left the wallet for the buy, including pool fees, network fee and Jito tip,
                excluding recoverable rent (token account creation).
* proceeds    = SOL that came back on a sell, net of network fee and tip, excluding rent refunds.
* cost_alloc  = cost_basis * sold / held (average cost), realized PnL = proceeds - cost_alloc.
Fills are idempotent (one row per verified child transaction).
"""
from __future__ import annotations

import time
from decimal import Decimal

from .db import Database


class PositionManager:
    def __init__(self, db: Database, wallet: str):
        self.db = db
        self.wallet = wallet

    def open_position(self, mint: str) -> dict | None:
        return self.db.one("SELECT * FROM positions WHERE mint=? AND wallet=? AND status='OPEN' ORDER BY id DESC LIMIT 1",
                           (mint, self.wallet))

    def get(self, pos_id: int) -> dict | None:
        return self.db.get("positions", pos_id)

    def list_open(self) -> list[dict]:
        return self.db.query("SELECT * FROM positions WHERE wallet=? AND status='OPEN' ORDER BY id", (self.wallet,))

    def _ensure(self, mint: str, route: str, decimals: int | None) -> dict:
        p = self.open_position(mint)
        if p:
            return p
        pid = self.db.insert("positions", {
            "mint": mint, "wallet": self.wallet, "status": "OPEN", "route": route, "decimals": decimals,
            "token_raw": "0", "cost_lamports": 0, "bought_raw_total": "0", "cost_total_lamports": 0,
            "sold_raw_total": "0", "proceeds_total_lamports": 0, "realized_pnl_lamports": 0, "fees_lamports": 0,
            "rent_lamports": 0, "tpsl_enabled": 0, "exit_state": "IDLE", "exit_attempts": 0,
            "opened_at": time.time(), "updated_at": time.time(),
        })
        return self.db.get("positions", pid)

    def apply_buy_fills(self, sub_id: int, mint: str, route: str, decimals: int | None,
                        tp_pct: str | None, sl_pct: str | None) -> dict | None:
        """Record every SUCCESS child tx of a BUY submission. Returns the position (or None)."""
        txs = self.db.query("SELECT * FROM sub_txs WHERE submission_id=? AND status='SUCCESS' ORDER BY idx", (sub_id,))
        if not txs:
            return None
        with self.db.transaction():
            pos = self._ensure(mint, route, decimals)
            for t in txs:
                if self.db.one("SELECT id FROM fills WHERE sub_tx_id=?", (t["id"],)):
                    continue  # idempotent
                tokens = int(t["actual_token_delta"] or 0)
                net = int(t["actual_sol_delta"] or 0) + int(t["rent_lamports"] or 0)
                cost = -net
                if tokens <= 0:
                    continue
                self.db.insert("fills", {
                    "position_id": pos["id"], "side": "BUY", "submission_id": sub_id, "sub_tx_id": t["id"],
                    "token_raw": str(tokens), "sol_lamports": cost, "fee_lamports": t["fee_lamports"],
                    "tip_lamports": t["tip_paid_lamports"], "swap_lamports": t["swap_lamports"],
                    "price": t["exec_price"], "created_at": time.time(),
                })
                pos = self.db.get("positions", pos["id"])
                self.db.update("positions", pos["id"], {
                    "token_raw": str(int(pos["token_raw"]) + tokens),
                    "cost_lamports": pos["cost_lamports"] + cost,
                    "bought_raw_total": str(int(pos["bought_raw_total"]) + tokens),
                    "cost_total_lamports": pos["cost_total_lamports"] + cost,
                    "fees_lamports": pos["fees_lamports"] + int(t["fee_lamports"] or 0) + int(t["tip_paid_lamports"] or 0),
                    "rent_lamports": pos["rent_lamports"] + max(int(t["rent_lamports"] or 0), 0),
                })
            tp = Decimal(tp_pct) if tp_pct else Decimal(0)
            sl = Decimal(sl_pct) if sl_pct else Decimal(0)
            if tp > 0 or sl > 0:
                self.db.update("positions", pos["id"], {
                    "tp_pct": str(tp) if tp > 0 else None, "sl_pct": str(sl) if sl > 0 else None,
                    "tpsl_enabled": 1, "exit_state": "IDLE", "exit_attempts": 0,
                })
        return self.db.get("positions", pos["id"])

    def apply_sell_fill(self, sell_id: int, sub_id: int) -> dict | None:
        """Apply a verified SELL; returns the updated sell row with PnL."""
        sell = self.db.get("sells", sell_id)
        txs = self.db.query("SELECT * FROM sub_txs WHERE submission_id=? AND status='SUCCESS'", (sub_id,))
        if not sell or not txs or sell["position_id"] is None:
            return sell
        t = txs[0]
        with self.db.transaction():
            if self.db.one("SELECT id FROM fills WHERE sub_tx_id=?", (t["id"],)):
                return self.db.get("sells", sell_id)
            pos = self.db.get("positions", sell["position_id"])
            sold = -int(t["actual_token_delta"] or 0)
            proceeds = int(t["actual_sol_delta"] or 0) + int(t["rent_lamports"] or 0)
            held = int(pos["token_raw"])
            alloc = pos["cost_lamports"] * min(sold, held) // held if held > 0 else 0
            pnl = proceeds - alloc
            remaining = max(held - sold, 0)
            self.db.insert("fills", {
                "position_id": pos["id"], "side": "SELL", "submission_id": sub_id, "sub_tx_id": t["id"],
                "token_raw": str(sold), "sol_lamports": proceeds, "fee_lamports": t["fee_lamports"],
                "tip_lamports": t["tip_paid_lamports"], "swap_lamports": t["swap_lamports"],
                "price": t["exec_price"], "created_at": time.time(),
            })
            upd = {
                "token_raw": str(remaining),
                "cost_lamports": pos["cost_lamports"] - alloc,
                "sold_raw_total": str(int(pos["sold_raw_total"]) + sold),
                "proceeds_total_lamports": pos["proceeds_total_lamports"] + proceeds,
                "realized_pnl_lamports": pos["realized_pnl_lamports"] + pnl,
                "fees_lamports": pos["fees_lamports"] + int(t["fee_lamports"] or 0) + int(t["tip_paid_lamports"] or 0),
            }
            if remaining == 0:
                upd.update({"status": "CLOSED", "closed_at": time.time(), "tpsl_enabled": 0, "exit_state": "IDLE"})
            self.db.update("positions", pos["id"], upd)
            self.db.update("sells", sell_id, {"proceeds_lamports": proceeds, "cost_alloc_lamports": alloc,
                                              "pnl_lamports": pnl, "amount_raw": str(sold)})
        return self.db.get("sells", sell_id)

    def set_tpsl(self, pos_id: int, tp_pct: Decimal | None = None, sl_pct: Decimal | None = None,
                 enabled: bool | None = None) -> dict:
        upd: dict = {}
        if tp_pct is not None:
            upd["tp_pct"] = str(tp_pct) if tp_pct > 0 else None
        if sl_pct is not None:
            upd["sl_pct"] = str(sl_pct) if sl_pct > 0 else None
        if enabled is not None:
            upd["tpsl_enabled"] = int(enabled)
            if enabled:
                upd["exit_state"] = "IDLE"
                upd["exit_attempts"] = 0
        self.db.update("positions", pos_id, upd)
        return self.db.get("positions", pos_id)

    @staticmethod
    def pnl_pct(cost_lamports: int, value_lamports: int) -> Decimal | None:
        if cost_lamports <= 0:
            return None
        return (Decimal(value_lamports - cost_lamports) / Decimal(cost_lamports) * 100).quantize(Decimal("0.01"))

    def avg_entry_price(self, pos: dict) -> Decimal | None:
        tok = int(pos["token_raw"])
        if tok <= 0 or pos["decimals"] is None:
            return None
        return Decimal(pos["cost_lamports"]) / Decimal(tok) * Decimal(10 ** pos["decimals"]) / Decimal(10**9)
