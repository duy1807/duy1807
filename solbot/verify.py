"""Transaction Verification & reconciliation.

Status ladder per child transaction:
    PREPARED -> SENT -> ACK -> PENDING -> LANDED -> SUCCESS | FAILED_SAFE
                                   \\-> UNKNOWN_HOLD (cannot be proven either way yet)

* SUCCESS needs on-chain evidence: a confirmed transaction with ``meta.err == null`` fetched with
  getTransaction, from which actual SOL spent / tokens received are computed. A second independent
  RPC is used to cross-check (verify_level 2); a contradiction puts the submission on HOLD.
* FAILED_SAFE = provably no effect: either landed with an error, or never landed and BOTH
  verification RPCs report a block height past the blockhash's last valid block height
  (an expired blockhash can never land).
* Everything else stays UNKNOWN_HOLD. HOLD is never cleared by force; the reconciler keeps checking
  (also after restarts) and no new transaction is created for that order meanwhile.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Awaitable, Callable

from solders.pubkey import Pubkey

from .db import Database
from .rpc import RpcError, RpcPool, RpcTransportError

log = logging.getLogger("solbot.verify")

ST_PREPARED = "PREPARED"
ST_SENT = "SENT"
ST_ACK = "ACK"
ST_PENDING = "PENDING"
ST_LANDED = "LANDED"
ST_SUCCESS = "SUCCESS"
ST_FAILED_SAFE = "FAILED_SAFE"
ST_HOLD = "UNKNOWN_HOLD"
ST_DRY_RUN = "DRY_RUN"
FINAL_TX = {ST_SUCCESS, ST_FAILED_SAFE}
OPEN_SUB = {ST_SENT, ST_ACK, ST_PENDING, ST_LANDED, ST_HOLD}


@dataclass
class Receipt:
    signature: str
    slot: int
    ok: bool
    err: str | None
    fee_lamports: int
    sol_delta: int  # payer post - pre (negative = spent)
    token_delta: int  # wallet's token balance change for the mint (raw)
    tip_paid: int
    rent_created: int
    rent_refund: int

    @property
    def net_lamports(self) -> int:
        """Wallet SOL change excluding recoverable rent movements (fees and tip included)."""
        return self.sol_delta + self.rent_created - self.rent_refund

    @property
    def swap_lamports(self) -> int:
        """SOL that went into (buy) or came out of (sell) the pool; network fee and tip removed."""
        if self.token_delta > 0:  # buy
            return -self.net_lamports - self.fee_lamports - self.tip_paid
        return self.net_lamports + self.fee_lamports + self.tip_paid


def parse_receipt(sig: str, tx: dict, wallet: Pubkey, mint: Pubkey, tip_account: Pubkey | None) -> Receipt:
    meta = tx.get("meta") or {}
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = list(msg.get("accountKeys") or [])
    loaded = meta.get("loadedAddresses") or {}
    keys += list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    w = str(wallet)
    if not keys or keys[0] != w:
        raise ValueError("wallet is not the fee payer of this transaction")
    sol_delta = int(post[0]) - int(pre[0])
    m = str(mint)

    def tok(entries):
        return sum(int(e["uiTokenAmount"]["amount"]) for e in entries or []
                   if e.get("mint") == m and e.get("owner") == w)

    token_delta = tok(meta.get("postTokenBalances")) - tok(meta.get("preTokenBalances"))
    tip_paid = 0
    tip_idx = None
    if tip_account is not None and str(tip_account) in keys:
        tip_idx = keys.index(str(tip_account))
        tip_paid = int(post[tip_idx]) - int(pre[tip_idx])
    rent_created = 0
    rent_refund = 0
    for i in range(1, min(len(pre), len(post))):
        if i == tip_idx:
            continue
        a, b = int(pre[i]), int(post[i])
        if a == 0 and b > 0:
            rent_created += b
        elif a > 0 and b == 0 and _is_owned_by_wallet_closed(i, keys, meta, w):
            rent_refund += a
    err = meta.get("err")
    return Receipt(sig, int(tx.get("slot") or 0), err is None, None if err is None else json.dumps(err),
                   int(meta.get("fee") or 0), sol_delta, token_delta, max(tip_paid, 0), rent_created, rent_refund)


def _is_owned_by_wallet_closed(i: int, keys: list, meta: dict, w: str) -> bool:
    # A pre-existing token account of the wallet that was closed in this tx (e.g. ATA close on full sell).
    for e in meta.get("preTokenBalances") or []:
        if e.get("accountIndex") == i and e.get("owner") == w:
            return True
    return False


@dataclass
class TxState:
    status: str
    slot: int | None = None
    err: str | None = None
    receipt: Receipt | None = None
    verify_a: str | None = None
    verify_b: str | None = None
    level: int = 0


def _status_word(s: dict | None) -> str:
    if s is None:
        return "not_found"
    cs = s.get("confirmationStatus")
    if cs in ("confirmed", "finalized"):
        return "landed_err" if s.get("err") is not None else "landed"
    return "processed"


FinalCallback = Callable[[int], Awaitable[None]]


class Verifier:
    def __init__(self, db: Database, rpc_a: RpcPool, rpc_b: RpcPool, poll_ms: int = 400,
                 margin_blocks: int = 5, independent: bool = True):
        self.db = db
        self.rpc_a = rpc_a
        self.rpc_b = rpc_b
        self.poll_ms = poll_ms
        self.margin = margin_blocks
        self.independent = independent
        self.on_final: list[FinalCallback] = []
        self._active: set[int] = set()

    async def _statuses(self, rpc: RpcPool, sigs: list[str]) -> list[dict | None] | None:
        try:
            return await rpc.get_signature_statuses(sigs)
        except (RpcError, RpcTransportError) as e:
            log.debug("signature status failed: %s", e)
            return None

    async def _height(self, rpc: RpcPool) -> int | None:
        try:
            return await rpc.get_block_height("confirmed")
        except (RpcError, RpcTransportError):
            return None

    async def check_once(self, sub: dict, txs: list[dict]) -> dict[int, TxState]:
        """One verification pass over the non-final child transactions of a submission."""
        open_txs = [t for t in txs if t["status"] not in FINAL_TX]
        out: dict[int, TxState] = {}
        if not open_txs:
            return out
        sigs = [t["signature"] for t in open_txs]
        sa, sb = await asyncio.gather(self._statuses(self.rpc_a, sigs), self._statuses(self.rpc_b, sigs))
        need_expiry = False
        for i, t in enumerate(open_txs):
            a = sa[i] if sa is not None else None
            b = sb[i] if sb is not None else None
            wa = _status_word(a) if sa is not None else "rpc_error"
            wb = _status_word(b) if sb is not None else "rpc_error"
            st = TxState(ST_PENDING, verify_a=wa, verify_b=wb)
            if "landed" in (wa, wb) or "landed_err" in (wa, wb):
                if {wa, wb} == {"landed", "landed_err"}:
                    st.status = ST_HOLD
                    st.err = "verification RPCs disagree on execution result"
                else:
                    src = self.rpc_a if wa.startswith("landed") else self.rpc_b
                    st = await self._landed(sub, t, src, st)
                    other = wb if src is self.rpc_a else wa
                    st.level = 2 if other.startswith("landed") and self.independent else 1
            elif wa == "processed" or wb == "processed":
                st.status = ST_PENDING  # seen at processed; wait for confirmed
            else:
                need_expiry = True
            out[t["id"]] = st
        if need_expiry:
            ha, hb = await asyncio.gather(self._height(self.rpc_a), self._height(self.rpc_b))
            lv = sub.get("last_valid_block_height") or 0
            expired = (ha is not None and hb is not None and lv > 0
                       and ha > lv + self.margin and hb > lv + self.margin)
            for t in open_txs:
                st = out[t["id"]]
                if st.status == ST_PENDING and st.verify_a in ("not_found",) and st.verify_b in ("not_found",):
                    if expired:
                        st.status = ST_FAILED_SAFE
                        st.err = f"not landed; blockhash expired (height {ha}/{hb} > {lv})"
                        st.level = 2 if self.independent else 1
        return out

    async def _landed(self, sub: dict, t: dict, rpc: RpcPool, st: TxState) -> TxState:
        try:
            tx = await rpc.get_transaction(t["signature"], "confirmed")
        except (RpcError, RpcTransportError) as e:
            st.status = ST_LANDED
            st.err = f"getTransaction failed: {e}"
            return st
        if tx is None:
            st.status = ST_LANDED  # status seen but full tx not yet served; retry next pass
            return st
        wallet = Pubkey.from_string(sub["wallet"])
        mint = Pubkey.from_string(sub["mint"])
        tip = Pubkey.from_string(sub["tip_account"]) if (t.get("has_tip") and sub.get("tip_account")) else None
        try:
            rc = parse_receipt(t["signature"], tx, wallet, mint, tip)
        except (ValueError, KeyError, TypeError) as e:
            st.status = ST_HOLD
            st.err = f"receipt unparsable: {e}"
            return st
        st.receipt = rc
        st.slot = rc.slot
        if rc.ok:
            st.status = ST_SUCCESS
        else:
            st.status = ST_FAILED_SAFE
            st.err = f"landed with error {rc.err}"
        return st

    def persist(self, sub_id: int, states: dict[int, TxState], txs: dict[int, dict]) -> None:
        for tx_id, st in states.items():
            fields: dict = {"status": st.status, "verify_a": st.verify_a, "verify_b": st.verify_b}
            if st.err:
                fields["err"] = st.err[:500]
            if st.slot:
                fields["slot"] = st.slot
            rc = st.receipt
            if rc is not None:
                t = txs[tx_id]
                swap = rc.swap_lamports
                tok = abs(rc.token_delta)
                price = None
                if tok > 0 and t.get("decimals") is not None:
                    price = str(Decimal(swap) / Decimal(tok) * Decimal(10 ** t["decimals"]) / Decimal(10**9))
                fields.update({
                    "actual_sol_delta": rc.sol_delta, "actual_token_delta": str(rc.token_delta),
                    "fee_lamports": rc.fee_lamports, "tip_paid_lamports": rc.tip_paid,
                    "rent_lamports": rc.rent_created - rc.rent_refund, "swap_lamports": swap,
                    "exec_price": price, "receipt_json": json.dumps(asdict(rc)),
                })
            self.db.update("sub_txs", tx_id, fields)

    @staticmethod
    def aggregate(statuses: list[str]) -> str:
        s = set(statuses)
        if s <= {ST_SUCCESS}:
            return ST_SUCCESS
        if s <= {ST_FAILED_SAFE}:
            return ST_FAILED_SAFE
        if s <= FINAL_TX:
            return ST_SUCCESS  # partial: some legs succeeded, others provably did not
        if ST_HOLD in s:
            return ST_HOLD
        if ST_LANDED in s:
            return ST_LANDED
        return ST_PENDING

    def load(self, sub_id: int) -> tuple[dict, list[dict]]:
        sub = self.db.get("submissions", sub_id)
        assert sub is not None
        txs = self.db.query("SELECT * FROM sub_txs WHERE submission_id=? ORDER BY idx", (sub_id,))
        tok = self.db.one("SELECT decimals FROM tokens WHERE mint=?", (sub["mint"],))
        w = self.db.one("SELECT pubkey FROM wallets ORDER BY id LIMIT 1")
        sub["wallet"] = w["pubkey"] if w else None
        for t in txs:
            t["decimals"] = tok["decimals"] if tok else None
        return sub, txs

    async def run(self, sub_id: int, deadline_s: float = 120.0) -> str:
        """Verify until every child tx is final or the deadline passes (then HOLD)."""
        if sub_id in self._active:
            return "ALREADY_RUNNING"
        self._active.add(sub_id)
        try:
            prev_status = (self.db.get("submissions", sub_id) or {}).get("status")
            t_end = time.monotonic() + deadline_s
            first_landed = False
            while True:
                sub, txs = self.load(sub_id)
                states = await self.check_once(sub, txs)
                self.persist(sub_id, states, {t["id"]: t for t in txs})
                sub, txs = self.load(sub_id)
                agg = self.aggregate([t["status"] for t in txs])
                if not first_landed and any(t["status"] in (ST_LANDED, ST_SUCCESS) for t in txs):
                    first_landed = True
                    self.db.update("submissions", sub_id, {"t_landed": time.time(),
                                                           "landed_slot": max((t["slot"] or 0) for t in txs)})
                if agg in (ST_SUCCESS, ST_FAILED_SAFE):
                    levels = [s.level for s in states.values()] or [0]
                    self.db.update("submissions", sub_id, {
                        "status": agg, "t_verified": time.time(), "verify_level": min(levels),
                        "error": "; ".join(t["err"] for t in txs if t.get("err"))[:500] or None,
                    })
                    for cb in self.on_final:
                        await cb(sub_id)
                    return agg
                if agg == ST_HOLD or time.monotonic() > t_end:
                    self.db.update("submissions", sub_id, {"status": ST_HOLD})
                    for t in txs:
                        if t["status"] not in FINAL_TX:
                            self.db.update("sub_txs", t["id"], {"status": ST_HOLD})
                    if prev_status != ST_HOLD:  # notify once per transition, not on every reconcile pass
                        self.db.log_event("WARN", "verify", "submission on UNKNOWN/HOLD", ref=f"sub:{sub_id}")
                        for cb in self.on_final:
                            await cb(sub_id)
                    return ST_HOLD
                if sub["status"] in (ST_SENT, ST_ACK) and agg == ST_PENDING:
                    self.db.update("submissions", sub_id, {"status": ST_PENDING})
                await asyncio.sleep(self.poll_ms / 1000)
        finally:
            self._active.discard(sub_id)
