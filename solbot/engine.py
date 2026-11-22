"""Unified trading engine.

BUY NGAY, FIXED DIP and LIMIT share *one* pipeline and differ only in when it is started
(and whether a price ceiling applies):

    plan legs sequentially (×1/×3/×5)  ->  risk checks  ->  build + sign  ->  freshness check
    ->  simulate (single / sequential bundle)  ->  freshness check  ->  persist SENT  ->  Jito
    ->  ACK  ->  on-chain verification (async)  ->  position / TP-SL

Price ceiling (DIP/LIMIT): each leg i is planned on the pool state left by legs 0..i-1. The leg's
expected execution price (all pool fees included) must be <= the locked trigger price, and the
on-chain guard ``max_quote_amount_in <= floor(base_out * ceiling)`` makes it impossible for that leg
to execute above the ceiling whatever the slippage setting (PumpSwap buy_v2 is exact-out).
For Jupiter the equivalent guard is ``minOut >= ceil(spend / ceiling)`` and the encoded minimum
is decoded from the built instruction before sending.
"""
from __future__ import annotations

import asyncio
import logging
import re
import random
import time
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Awaitable, Callable

from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.instruction import Instruction
from solders.pubkey import Pubkey

from . import constants as C
from .config import Config
from .db import Database
from .dex import pumpswap as ps
from .dex.jupiter import JupiterClient, JupiterError, encoded_min_out, quote_with_min_out
from .jito import JitoClient
from .market import ROUTE_JUPITER, ROUTE_PUMPSWAP, BlockhashCache, MarketEngine, MarketError, Snapshot, StaleData
from .positions import PositionManager
from .risk import RiskManager, RiskReject
from .rpc import RpcError, RpcPool, RpcTransportError, token_account_amount
from .settings import MODE_LIVE, Settings
from .simulation import METHOD_NONE, SimExpect, Simulator
from .tx_builder import (BuiltTx, TxTooLarge, ata, close_account, compile_and_sign, compute_budget,
                         create_ata_idempotent, decode_lookup_table, sol_transfer, wsol_unwrap, wsol_wrap)
from .verify import ST_DRY_RUN, ST_FAILED_SAFE, ST_HOLD, ST_PREPARED, ST_SENT, ST_ACK, ST_SUCCESS, Verifier
from .wallet import Wallet

log = logging.getLogger("solbot.engine")

ORDER_ARMED = "ARMED"
ORDER_TRIGGERED = "TRIGGERED"
ORDER_SENT = "SENT"
ORDER_FILLED = "FILLED"
ORDER_FAILED = "FAILED_SAFE"
ORDER_HOLD = "HOLD"
ORDER_CANCELLED = "CANCELLED"
ORDER_DRY_RUN = "DRY_RUN"
ORDER_TERMINAL = {ORDER_FILLED, ORDER_FAILED, ORDER_CANCELLED, ORDER_DRY_RUN}

MAX_PREPARE_ROUNDS = 3
MAX_INFRA_REARMS = 5


class PriceCapNotMet(Exception):
    """The ceiling cannot be honoured on the current state: nothing was sent (order stays ARMED)."""


class PresendError(Exception):
    pass


@dataclass
class Leg:
    idx: int
    spend_lamports: int  # planned SOL into the pool (all pool fees included)
    base_out: int  # planned tokens (exact for PumpSwap, expected for Jupiter)
    max_in: int  # worst-case SOL into the pool for this leg (PumpSwap) / exact (Jupiter)
    min_out: int  # worst-case tokens (== base_out for PumpSwap exact-out)
    planned_price: Decimal  # SOL/token incl. pool fees
    cap_price: Decimal | None
    route_data: dict = field(default_factory=dict)


@dataclass
class Plan:
    route: str
    mint: Pubkey
    decimals: int
    token_program: Pubkey
    legs: list[Leg]
    snapshot: Snapshot
    cu_limit: int
    note: str = ""

    @property
    def max_ins(self) -> list[int]:
        return [l.max_in for l in self.legs]


@dataclass
class ExecResult:
    status: str  # REARM | PRESEND_FAILED | DRY_RUN | SENT
    submission_id: int | None = None
    message: str = ""


def sol_str(lamports: int | None, digits: int = 6) -> str:
    if lamports is None:
        return "—"
    return f"{Decimal(lamports) / Decimal(C.LAMPORTS_PER_SOL):.{digits}f}"


def price_from(lamports: int, raw: int, decimals: int) -> Decimal:
    if raw <= 0:
        return Decimal(0)
    return Decimal(lamports) / Decimal(raw) * Decimal(10**decimals) / Decimal(C.LAMPORTS_PER_SOL)


def cap_lamports(base_out: int, cap_price: Decimal, decimals: int) -> int:
    """Max lamports allowed for ``base_out`` raw tokens at ``cap_price`` SOL/token (floor)."""
    v = Decimal(base_out) * cap_price * Decimal(C.LAMPORTS_PER_SOL) / Decimal(10**decimals)
    return int(v.to_integral_value(rounding=ROUND_FLOOR))


def min_tokens_for(spend: int, cap_price: Decimal, decimals: int) -> int:
    """Fewest raw tokens so that spend / tokens <= cap_price (ceil)."""
    v = Decimal(spend) * Decimal(10**decimals) / (cap_price * Decimal(C.LAMPORTS_PER_SOL))
    return int(v.to_integral_value(rounding=ROUND_CEILING))


Notify = Callable[[int | None, str], None]


class TradeEngine:
    def __init__(self, *, config: Config, settings: Settings, db: Database, wallet: Wallet, rpc: RpcPool,
                 market: MarketEngine, blockhash: BlockhashCache, simulator: Simulator, jito: JitoClient,
                 verifier: Verifier, risk: RiskManager, positions: PositionManager,
                 jupiter: JupiterClient | None = None):
        self.config = config
        self.settings = settings
        self.db = db
        self.wallet = wallet
        self.rpc = rpc
        self.market = market
        self.blockhash = blockhash
        self.simulator = simulator
        self.jito = jito
        self.verifier = verifier
        self.risk = risk
        self.positions = positions
        self.jupiter = jupiter
        self.notify: Notify = lambda chat, text: None
        self.on_position_opened: Callable[[dict], Awaitable[None]] | None = None
        self.on_sell_final: Callable[[dict], Awaitable[None]] | None = None
        self._mint_locks: dict[str, asyncio.Lock] = {}
        self._alt_cache: dict[Pubkey, AddressLookupTableAccount] = {}
        self._ua_cache: dict[str, tuple[float, dict]] = {}
        self._tasks: set[asyncio.Task] = set()
        verifier.on_final.append(self._on_submission_final)
        if not db.one("SELECT id FROM wallets WHERE pubkey=?", (str(wallet.pubkey),)):
            db.insert("wallets", {"pubkey": str(wallet.pubkey), "label": "main", "created_at": time.time()})

    def _cu_nonce(self) -> int:
        """Small per-transaction CU-limit offset so two otherwise identical transactions (same order
        parameters, same blockhash) never share a signature — identical signatures would be
        deduplicated by the network and silently execute only once."""
        self._nonce = (getattr(self, "_nonce", random.randrange(4096)) + 1) % 4096
        return self._nonce + 1

    def _sig_used(self, sig: str) -> bool:
        return self.db.one("SELECT id FROM sub_txs WHERE signature=?", (sig,)) is not None

    def _sign_unique(self, ixs_fn, blockhash: str, alts, tip: int, tip_account, cu_base: int, micro: int) -> BuiltTx:
        for _ in range(8):
            cu = cu_base + self._cu_nonce()
            tx = compile_and_sign(self.wallet.signer, ixs_fn(cu), blockhash, alts, tip, tip_account, cu, micro)
            if not self._sig_used(tx.signature):
                return tx
        raise PresendError("không tạo được chữ ký giao dịch duy nhất")

    def _lock(self, mint: str) -> asyncio.Lock:
        return self._mint_locks.setdefault(mint, asyncio.Lock())

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    # ======================================================================== planning ====
    def plan_pumpswap_buy(self, snap: Snapshot, count: int, sol_per_buy: int, slippage_bps: int,
                          cap_price: Decimal | None, cu_limit: int) -> Plan:
        st = snap.ps_state
        assert st is not None
        legs: list[Leg] = []
        for i in range(count):
            cost = st.quote_buy_for_sol(sol_per_buy)
            planned_price = price_from(cost.total, cost.base_out, st.base_decimals)
            max_in = sol_per_buy * (10_000 + slippage_bps) // 10_000
            if cap_price is not None:
                cap = cap_lamports(cost.base_out, cap_price, st.base_decimals)
                if cost.total > cap:
                    raise PriceCapNotMet(
                        f"leg {i + 1}/{count}: giá thực hiện dự kiến {planned_price:.12f} > trần {cap_price:.12f}")
                max_in = min(max_in, cap)
            max_in = max(max_in, cost.total)
            legs.append(Leg(i, cost.total, cost.base_out, max_in, cost.base_out, planned_price, cap_price,
                            {"quote_amount_in": cost.quote_amount_in}))
            st = st.after_buy(cost)
        return Plan(ROUTE_PUMPSWAP, snap.mint, snap.ps_state.base_decimals, snap.ps_state.base_token_program,
                    legs, snap, cu_limit)

    async def plan_jupiter_buy(self, snap: Snapshot, decimals: int, token_program: Pubkey, count: int,
                               sol_per_buy: int, slippage_bps: int, cap_price: Decimal | None,
                               cu_limit: int) -> Plan:
        if self.jupiter is None:
            raise PresendError("Jupiter chưa được cấu hình")
        quotes = await asyncio.gather(*[
            self.jupiter.quote(C.WSOL_MINT, snap.mint, sol_per_buy * k, slippage_bps) for k in range(1, count + 1)])
        legs = []
        prev = 0
        for i, q in enumerate(quotes):
            marginal = q.out_amount - prev
            prev = q.out_amount
            if marginal <= 0:
                raise PresendError("Jupiter không trả được lượng token cho leg tuần tự")
            planned_price = price_from(sol_per_buy, marginal, decimals)
            min_out = marginal * (10_000 - slippage_bps) // 10_000
            if cap_price is not None:
                need = min_tokens_for(sol_per_buy, cap_price, decimals)
                if marginal < need:
                    raise PriceCapNotMet(
                        f"leg {i + 1}/{count}: giá dự kiến {planned_price:.12f} > trần {cap_price:.12f}")
                min_out = max(min_out, need)
            legs.append(Leg(i, sol_per_buy, marginal, sol_per_buy, max(min_out, 1), planned_price, cap_price,
                            {"quote": quotes[0].raw}))
        return Plan(ROUTE_JUPITER, snap.mint, decimals, token_program, legs, snap, cu_limit)

    async def plan_buy(self, order: dict, snap: Snapshot) -> Plan:
        cap = Decimal(order["trigger_price"]) if order["kind"] in ("DIP", "LIMIT") else None
        if snap.route == ROUTE_PUMPSWAP:
            return self.plan_pumpswap_buy(snap, order["count"], order["sol_per_buy_lamports"],
                                          order["slippage_bps"], cap, order["cu_limit"])
        m = self.market.markets[snap.mint]
        return await self.plan_jupiter_buy(snap, m.info.decimals, m.info.token_program, order["count"],
                                           order["sol_per_buy_lamports"], order["slippage_bps"], cap,
                                           order["cu_limit"])

    # ======================================================================== building ====
    async def _user_accounts(self, mint: Pubkey, token_program: Pubkey, max_age_ms: float = 0) -> dict:
        """Wallet SOL, token ATA and volume-accumulator state. ``max_age_ms`` > 0 allows a recent cached
        read (DIP/LIMIT hot path; prefetched while armed). Any submission on the mint invalidates the cache;
        the simulation still checks the real balances before anything is sent."""
        key = str(mint)
        if max_age_ms > 0:
            hit = self._ua_cache.get(key)
            if hit and (time.monotonic() - hit[0]) * 1000 <= max_age_ms:
                return dict(hit[1])
        user = self.wallet.pubkey
        base_ata = ata(user, mint, token_program)
        _, (w, b, uva) = await self.rpc.get_multiple_accounts(
            [user, base_ata, C.user_volume_accumulator(user)], "processed")
        res = {
            "wallet_lamports": w.lamports if w else 0,
            "base_ata": base_ata,
            "base_ata_exists": b is not None,
            "base_amount": token_account_amount(b.data) if b is not None and len(b.data) >= 72 else 0,
            "uva_exists": uva is not None,
        }
        self._ua_cache[key] = (time.monotonic(), res)
        return dict(res)

    def invalidate_user_accounts(self, mint: str | None = None) -> None:
        if mint is None:
            self._ua_cache.clear()
        else:
            self._ua_cache.pop(mint, None)

    async def prefetch_user_accounts(self, mint: Pubkey, older_than_ms: float = 1000) -> None:
        hit = self._ua_cache.get(str(mint))
        if hit and (time.monotonic() - hit[0]) * 1000 < older_than_ms:
            return
        m = self.market.markets.get(mint)
        if m is not None:
            await self._user_accounts(mint, m.info.token_program)

    def _buyback_ata(self) -> Pubkey:
        g = self.market.global_config
        rec = list(g.buyback_fee_recipients) if g and g.buyback_fee_recipients else C.BUYBACK_FEE_RECIPIENTS
        rec = [r for r in rec if r != Pubkey.default()] or C.BUYBACK_FEE_RECIPIENTS
        return ata(random.choice(rec), C.WSOL_MINT, C.TOKEN_PROGRAM)

    async def _lookup_tables(self, keys: list[Pubkey]) -> list[AddressLookupTableAccount]:
        missing = [k for k in keys if k not in self._alt_cache]
        if missing:
            _, accs = await self.rpc.get_multiple_accounts(missing, "confirmed")
            for k, a in zip(missing, accs):
                if a is None:
                    raise PresendError(f"lookup table {k} not found")
                self._alt_cache[k] = decode_lookup_table(k, a.data)
        return [self._alt_cache[k] for k in keys]

    def pumpswap_buy_ixs(self, plan: Plan, leg: Leg, user: Pubkey) -> list[Instruction]:
        """Swap instructions of one PumpSwap BUY leg (no compute budget / tip). Shared with the mainnet probe."""
        st = plan.snapshot.ps_state
        return [
            create_ata_idempotent(user, user, plan.mint, plan.token_program),
            *wsol_wrap(user, leg.max_in),
            ps.build_buy_v2(user, st.pool, plan.token_program, C.TOKEN_PROGRAM, ata(user, plan.mint, plan.token_program),
                            ata(user, C.WSOL_MINT), self._buyback_ata(), leg.base_out, leg.max_in),
            wsol_unwrap(user),
        ]

    def pumpswap_sell_ixs(self, st: ps.PumpSwapState, user: Pubkey, token_program: Pubkey, amount: int,
                          min_out: int, close_ata: bool = False) -> list[Instruction]:
        """Swap instructions of a PumpSwap SELL (no compute budget / tip). Shared with the mainnet probe."""
        base_ata = ata(user, st.pool.base_mint, token_program)
        ixs = [
            create_ata_idempotent(user, user, C.WSOL_MINT),
            ps.build_sell_v2(user, st.pool, token_program, C.TOKEN_PROGRAM, base_ata, ata(user, C.WSOL_MINT),
                             self._buyback_ata(), amount, min_out),
            wsol_unwrap(user),
        ]
        if close_ata:
            ixs.append(close_account(base_ata, user, user, token_program))
        return ixs

    async def build_buy_txs(self, plan: Plan, blockhash: str, micro: int, tip: int, tip_account: Pubkey,
                            user_accts: dict) -> list[BuiltTx]:
        user = self.wallet.pubkey
        out: list[BuiltTx] = []
        n = len(plan.legs)
        used: set[str] = set()
        for leg in plan.legs:
            last = leg.idx == n - 1
            ixs: list[Instruction] = []
            alts: list[AddressLookupTableAccount] = []
            if plan.route == ROUTE_PUMPSWAP:
                ixs += self.pumpswap_buy_ixs(plan, leg, user)
            else:
                assert self.jupiter is not None
                q = quote_with_min_out(leg.route_data["quote"], leg.min_out)
                sw = await self.jupiter.swap_instructions(q, user)
                enc = encoded_min_out(sw.swap, leg.spend_lamports)
                if enc is None:
                    if leg.cap_price is not None:
                        raise PresendError("Không giải mã được minOut của lệnh Jupiter — không thể bảo đảm trần giá")
                elif enc < leg.min_out:
                    raise PresendError(f"minOut Jupiter {enc} < yêu cầu {leg.min_out}")
                leg.route_data["encoded_min_out"] = enc
                ixs += sw.setup + [sw.swap] + sw.cleanup + sw.other
                alts = await self._lookup_tables(sw.lookup_tables)
            if last:
                ixs.append(sol_transfer(user, tip_account, tip))
            body = list(ixs)
            tx = self._sign_unique(lambda cu, body=body: compute_budget(cu, micro) + body, blockhash, alts,
                                   tip if last else 0, tip_account if last else None, plan.cu_limit, micro)
            if tx.signature in used:
                raise PresendError("chữ ký trùng trong bundle")
            used.add(tx.signature)
            out.append(tx)
        return out

    # ======================================================================= execution ====
    async def execute_buy(self, order_id: int, signal_ms: float | None = None,
                          trigger_snapshot: Snapshot | None = None) -> ExecResult:
        """Run the shared BUY pipeline for an order already claimed (status TRIGGERED)."""
        order = self.db.get("orders", order_id)
        assert order is not None
        mint = Pubkey.from_string(order["mint"])
        t_signal = time.time() if signal_ms is None else signal_ms / 1000
        async with self._lock(order["mint"]):
            try:
                res = await self._prepare_and_send_buy(order, mint, t_signal, trigger_snapshot)
            except PriceCapNotMet as e:
                self.db.update("orders", order_id, {"wait_note": str(e)[:300]})
                return ExecResult("REARM", None, str(e))
            except (StaleData, RpcTransportError, RpcError) as e:
                # infrastructure problem BEFORE anything was sent: a DIP/LIMIT order goes back to ARMED
                # (bounded) instead of being consumed; BUY NGAY reports the failure to the user.
                order = self.db.get("orders", order_id)
                if order["kind"] in ("DIP", "LIMIT") and order["infra_rearms"] < MAX_INFRA_REARMS:
                    self.db.update("orders", order_id, {"infra_rearms": order["infra_rearms"] + 1,
                                                        "wait_note": f"hạ tầng: {e}"[:300]})
                    return ExecResult("REARM", None, f"infra: {e}")
                msg = f"{type(e).__name__}: {e}"
                self.db.update("orders", order_id, {"status": ORDER_FAILED, "status_reason": msg[:500],
                                                    "finished_at": time.time()})
                self.notify(order["chat_id"], f"⛔ Lệnh #{order_id} không gửi (lỗi hạ tầng): {e}")
                return ExecResult("PRESEND_FAILED", None, msg)
            except (RiskReject, PresendError, MarketError, ps.QuoteError, JupiterError, TxTooLarge) as e:
                msg = f"{type(e).__name__}: {e}"
                self.db.update("orders", order_id, {"status": ORDER_FAILED, "status_reason": msg[:500],
                                                    "finished_at": time.time()})
                self.db.log_event("WARN", "buy", f"order {order_id} pre-send failure: {msg}", ref=f"order:{order_id}")
                self.notify(order["chat_id"], f"⛔ Lệnh #{order_id} ({order['kind']} ×{order['count']}) không gửi: {e}")
                return ExecResult("PRESEND_FAILED", None, msg)
            except Exception as e:  # unexpected: nothing was sent (send happens last)
                log.exception("unexpected buy error")
                msg = f"internal error: {type(e).__name__}: {e}"
                self.db.update("orders", order_id, {"status": ORDER_FAILED, "status_reason": msg[:500],
                                                    "finished_at": time.time()})
                self.notify(order["chat_id"], f"⛔ Lệnh #{order_id} lỗi nội bộ trước khi gửi: {e}")
                return ExecResult("PRESEND_FAILED", None, msg)
        return res

    async def _prepare_and_send_buy(self, order: dict, mint: Pubkey, t_signal: float,
                                    trigger_snapshot: Snapshot | None) -> ExecResult:
        micro = order["priority_micro_lamports"]
        tip = order["tip_lamports"]
        last_err = ""
        for rnd in range(MAX_PREPARE_ROUNDS):
            reuse_ms = self.config.trigger_snapshot_reuse_ms
            snap = trigger_snapshot if (rnd == 0 and trigger_snapshot is not None
                                        and trigger_snapshot.age_ms() <= reuse_ms) else None
            m = self.market.markets.get(mint) or await self.market.resolve(mint)
            if snap is None:
                # snapshot (re-fetched if older than reuse_ms, so the plan matches the bank the simulation
                # will see) and wallet state in parallel: one round-trip instead of two
                ua_age = 2000 if trigger_snapshot is not None else 0
                snap, accts = await asyncio.gather(self.market.fresh_snapshot(mint, max_age_ms=2 * reuse_ms),
                                                   self._user_accounts(mint, m.info.token_program, max_age_ms=ua_age))
            else:
                # trigger path: snapshot came from the stream, wallet state usually prefetched while ARMED
                accts = await self._user_accounts(mint, m.info.token_program, max_age_ms=2000)
            if order["kind"] in ("DIP", "LIMIT") and snap.price_sol > Decimal(order["trigger_price"]):
                raise PriceCapNotMet(f"giá hiện tại {snap.price_sol:.12f} > giá kích hoạt {order['trigger_price']}")
            plan: Plan = await self.plan_buy(order, snap)
            t_planned = time.time()
            st = snap.ps_state
            self.risk.check_buy(
                mint=order["mint"], exec_mode=order["exec_mode"], count=order["count"],
                sol_per_tx=order["sol_per_buy_lamports"], max_ins=plan.max_ins, slippage_bps=order["slippage_bps"],
                tip=tip, micro_lamports=micro, cu_limit=plan.cu_limit, snapshot_age_ms=snap.age_ms(),
                wallet_lamports=accts["wallet_lamports"], base_ata_exists=accts["base_ata_exists"],
                token_2022=m.info.token_program == C.TOKEN_2022_PROGRAM, uva_exists=accts["uva_exists"],
                buy_disabled=bool(st and st.global_config and st.global_config.buy_disabled),
            )
            bh, lvbh, bh_slot, bh_age = await self.blockhash.get()
            tip_account = self.jito.next_tip_account()
            txs = await self.build_buy_txs(plan, bh, micro, tip, tip_account, accts)
            t_signed = time.time()
            if not self.market.is_current(snap):
                last_err = f"snapshot {snap.age_ms():.0f} ms sau khi ký — chuẩn bị lại"
                continue
            expects = []
            acc = accts["base_amount"]
            for leg in plan.legs:
                acc += leg.min_out
                expects.append(SimExpect(accts["base_ata"], acc))
            sim = await self.simulator.simulate(txs, expects, min_slot=snap.slot or None)
            t_sim = time.time()
            if not sim.ok and _blockhash_problem(sim.err):
                await self.blockhash.refresh()
                last_err = "blockhash hết hạn khi mô phỏng — chuẩn bị lại với blockhash mới"
                continue
            if not sim.ok and price_guard_error(sim.err, plan.route):
                # the pool moved between snapshot and simulation and the on-chain guard (maxIn / minOut)
                # rejected it. Nothing was sent.
                if order["kind"] in ("DIP", "LIMIT"):
                    raise PriceCapNotMet("giá trên chuỗi đã bật lên vượt trần lúc mô phỏng (ExceededSlippage) — "
                                         "chưa gửi gì, tiếp tục chờ giá ≤ trần")
                raise PresendError(f"giá biến động vượt slippage {order['slippage_bps'] / 100:g}% lúc mô phỏng "
                                   f"(ExceededSlippage) — không mua, không gửi gì. Thử lại hoặc tăng slippage.")
            if not self.market.is_current(snap):
                last_err = f"snapshot {snap.age_ms():.0f} ms sau mô phỏng — chuẩn bị lại"
                continue
            return await self._persist_and_send(
                purpose="BUY", order=order, sell=None, plan=plan, txs=txs, sim=sim, blockhash=bh, lvbh=lvbh,
                tip=tip, tip_account=tip_account, micro=micro, snap=snap,
                times={"t_signal": t_signal, "t_planned": t_planned, "t_signed": t_signed, "t_simulated": t_sim})
        raise StaleData(f"không có dữ liệu đủ mới sau {MAX_PREPARE_ROUNDS} lần chuẩn bị: {last_err}")

    async def _persist_and_send(self, *, purpose: str, order: dict | None, sell: dict | None, plan: Plan,
                                txs: list[BuiltTx], sim, blockhash: str, lvbh: int, tip: int, tip_account: Pubkey,
                                micro: int, snap: Snapshot, times: dict) -> ExecResult:
        exec_mode = (order or sell)["exec_mode"]
        no_send = exec_mode != MODE_LIVE
        sim_ok = sim.ok
        if not sim_ok and not (no_send and sim.method == METHOD_NONE
                               and self.config.sim_allow_missing_bundle_sim_in_no_send):
            raise PresendError(f"Mô phỏng thất bại ({sim.method}): {sim.err}")
        if not no_send and not self.settings.is_live:
            raise PresendError("Bot không ở chế độ LIVE")
        sub = {
            "purpose": purpose, "order_id": order["id"] if order else None, "sell_id": sell["id"] if sell else None,
            "mint": str(plan.mint), "route": plan.route, "count": len(txs), "exec_mode": exec_mode,
            "status": ST_PREPARED, "blockhash": blockhash, "last_valid_block_height": lvbh,
            "snapshot_slot": snap.slot, "snapshot_age_ms": snap.age_ms(), "tip_lamports": tip,
            "tip_account": str(tip_account), "priority_micro_lamports": micro, "cu_limit": plan.cu_limit,
            "sim_method": sim.method, "sim_ok": int(sim_ok), "sim_detail": sim.summary()[:2000],
            "created_at": time.time(), "updated_at": time.time(), **times,
        }
        import base64
        import json

        self.invalidate_user_accounts(str(plan.mint))
        with self.db.transaction():
            sub_id = self.db.insert("submissions", sub)
            for leg, tx in zip(plan.legs, txs):
                self.db.insert("sub_txs", {
                    "submission_id": sub_id, "idx": leg.idx, "signature": tx.signature,
                    "raw_b64": base64.b64encode(tx.raw).decode(), "status": ST_PREPARED,
                    "planned_in_lamports": leg.spend_lamports, "planned_out_raw": str(leg.base_out),
                    "max_in_lamports": leg.max_in, "min_out_raw": str(leg.min_out),
                    "limit_price": str(leg.cap_price) if leg.cap_price is not None else None,
                    "planned_price": str(leg.planned_price), "has_tip": int(tx.tip_lamports > 0),
                    "updated_at": time.time(),
                })
            if order:
                self.db.update("orders", order["id"], {"submission_id": sub_id})
            if sell:
                self.db.update("sells", sell["id"], {"submission_id": sub_id})
        self.db.metric("prepare_ms", (times["t_simulated"] - times["t_signal"]) * 1000, f"sub:{sub_id}")
        self.db.metric("simulate_ms", sim.elapsed_ms, f"sub:{sub_id}")

        if no_send:
            self.db.update("submissions", sub_id, {"status": ST_DRY_RUN})
            self.db.execute("UPDATE sub_txs SET status=? WHERE submission_id=?", (ST_DRY_RUN, sub_id))
            note = sim.summary()
            if order:
                self.db.update("orders", order["id"], {"status": ORDER_DRY_RUN, "finished_at": time.time(),
                                                       "status_reason": f"NO_SEND: {note}"[:500]})
            if sell:
                self.db.update("sells", sell["id"], {"status": ORDER_DRY_RUN, "finished_at": time.time(),
                                                     "status_reason": f"NO_SEND: {note}"[:500]})
            return ExecResult("DRY_RUN", sub_id, note)

        # ---- LIVE: persist SENT (with signatures) BEFORE handing bytes to the network ----
        with self.db.transaction():
            self.db.update("submissions", sub_id, {"status": ST_SENT, "t_sent": time.time()})
            self.db.execute("UPDATE sub_txs SET status=? WHERE submission_id=?", (ST_SENT, sub_id))
            if order:
                self.db.update("orders", order["id"], {"status": ORDER_SENT})
            if sell:
                self.db.update("sells", sell["id"], {"status": ORDER_SENT})
        if len(txs) == 1:
            ack = await self.jito.send_transaction(txs[0].raw)
        else:
            ack = await self.jito.send_bundle([t.raw for t in txs])
        detail = {"ok": ack.ok, "http": ack.http_status, "detail": ack.detail, "attempts": ack.attempts,
                  "fanout": ack.fanout}
        upd = {"jito_endpoint": ack.endpoint, "ack_detail": json.dumps(detail)[:2000], "bundle_id": ack.bundle_id}
        if ack.ok:
            upd.update({"status": ST_ACK, "t_ack": time.time()})
            self.db.execute("UPDATE sub_txs SET status=? WHERE submission_id=?", (ST_ACK, sub_id))
            self.db.metric("jito_ack_ms", ack.ack_ms, f"sub:{sub_id}")
        else:
            # Not acknowledged. The bytes may still have reached Jito, so we do NOT declare failure here:
            # the verifier waits for landing or blockhash expiry (FAILED_SAFE) like any other submission.
            upd["error"] = f"Jito không ACK: {ack.detail}"[:500]
        self.db.update("submissions", sub_id, upd)
        self._spawn(self.verifier.run(sub_id, deadline_s=self.config.verify_deadline_s))
        return ExecResult("SENT", sub_id, "ACK" if ack.ok else f"no ACK: {ack.detail}")

    # ====================================================================== callbacks ====
    async def _on_submission_final(self, sub_id: int) -> None:
        sub = self.db.get("submissions", sub_id)
        if sub is None:
            return
        self.invalidate_user_accounts(sub["mint"])
        try:
            if sub["purpose"] == "BUY" and sub["order_id"]:
                await self._finish_buy(sub)
            elif sub["purpose"] == "SELL" and sub["sell_id"]:
                await self._finish_sell(sub)
        except Exception:
            log.exception("post-verification handling failed for sub %s", sub_id)

    async def _finish_buy(self, sub: dict) -> None:
        order = self.db.get("orders", sub["order_id"])
        txs = self.db.query("SELECT * FROM sub_txs WHERE submission_id=? ORDER BY idx", (sub["id"],))
        status = sub["status"]
        if status == ST_SUCCESS:
            tok = self.db.one("SELECT decimals FROM tokens WHERE mint=?", (sub["mint"],))
            pos = self.positions.apply_buy_fills(sub["id"], sub["mint"], sub["route"], tok["decimals"] if tok else None,
                                                 order["tp_pct"], order["sl_pct"])
            n_ok = sum(1 for t in txs if t["status"] == ST_SUCCESS)
            partial = n_ok < len(txs)
            self.db.update("orders", order["id"], {
                "status": ORDER_FILLED, "finished_at": time.time(),
                "status_reason": f"partial {n_ok}/{len(txs)}" if partial else None})
            if pos and self.on_position_opened:
                await self.on_position_opened(pos)
            self.notify(order["chat_id"], self.format_buy_report(order, sub, txs, pos))
        elif status == ST_FAILED_SAFE:
            self.db.update("orders", order["id"], {"status": ORDER_FAILED, "finished_at": time.time(),
                                                   "status_reason": (sub["error"] or "")[:500]})
            self.notify(order["chat_id"], self.format_buy_report(order, sub, txs, None))
        elif status == ST_HOLD:
            self.db.update("orders", order["id"], {"status": ORDER_HOLD,
                                                   "status_reason": "UNKNOWN/HOLD — đang đối soát"})
            self.notify(order["chat_id"], f"⚠️ Lệnh #{order['id']} UNKNOWN/HOLD: chưa xác minh được trên chuỗi. "
                                          f"Bot KHÔNG gửi lại và tiếp tục đối soát.")

    async def _finish_sell(self, sub: dict) -> None:
        sell = self.db.get("sells", sub["sell_id"])
        status = sub["status"]
        if status == ST_SUCCESS:
            sell = self.positions.apply_sell_fill(sell["id"], sub["id"])
            self.db.update("sells", sell["id"], {"status": ORDER_FILLED, "finished_at": time.time()})
        elif status == ST_FAILED_SAFE:
            self.db.update("sells", sell["id"], {"status": ORDER_FAILED, "finished_at": time.time(),
                                                 "status_reason": (sub["error"] or "")[:500]})
        elif status == ST_HOLD:
            self.db.update("sells", sell["id"], {"status": ORDER_HOLD})
        sell = self.db.get("sells", sell["id"])
        txs = self.db.query("SELECT * FROM sub_txs WHERE submission_id=?", (sub["id"],))
        self.notify(sell["chat_id"], self.format_sell_report(sell, sub, txs))
        if self.on_sell_final:
            await self.on_sell_final(sell)

    # =========================================================================== SELL ====
    async def execute_sell(self, mint_s: str, pct: Decimal, reason: str, chat_id: int | None,
                           exec_mode: str | None = None) -> ExecResult:
        mint = Pubkey.from_string(mint_s)
        if not (Decimal(0) < pct <= Decimal(100)):
            return ExecResult("PRESEND_FAILED", None, "Tỷ lệ bán phải trong (0, 100]")
        pos = self.positions.open_position(mint_s)
        mode = exec_mode or self.settings.mode
        sell_id = self.db.insert("sells", {
            "position_id": pos["id"] if pos else None, "mint": mint_s, "route": "?", "pct": str(pct),
            "reason": reason, "exec_mode": mode, "status": ORDER_TRIGGERED,
            "slippage_bps": self.settings.sell_slippage_bps(), "chat_id": chat_id,
            "created_at": time.time(), "updated_at": time.time(),
        })
        async with self._lock(mint_s):
            try:
                return await self._prepare_and_send_sell(sell_id, mint, pct)
            except (RiskReject, PresendError, StaleData, MarketError, ps.QuoteError, JupiterError, TxTooLarge,
                    RpcTransportError, RpcError) as e:
                msg = f"{type(e).__name__}: {e}"
                self.db.update("sells", sell_id, {"status": ORDER_FAILED, "status_reason": msg[:500],
                                                  "finished_at": time.time()})
                self.notify(chat_id, f"⛔ SELL {pct}% không gửi: {e}")
                return ExecResult("PRESEND_FAILED", None, msg)
            except Exception as e:
                log.exception("unexpected sell error")
                self.db.update("sells", sell_id, {"status": ORDER_FAILED, "status_reason": str(e)[:500],
                                                  "finished_at": time.time()})
                self.notify(chat_id, f"⛔ SELL lỗi nội bộ trước khi gửi: {e}")
                return ExecResult("PRESEND_FAILED", None, str(e))

    async def _prepare_and_send_sell(self, sell_id: int, mint: Pubkey, pct: Decimal) -> ExecResult:
        sell = self.db.get("sells", sell_id)
        micro = self.settings.int("priority_micro_lamports")
        tip = self.settings.tip_lamports()
        slip = sell["slippage_bps"]
        t_signal = time.time()
        last_err = ""
        for _ in range(MAX_PREPARE_ROUNDS):
            snap = await self.market.fresh_snapshot(mint)
            m = self.market.markets[mint]
            accts = await self._user_accounts(mint, m.info.token_program)
            balance = accts["base_amount"]
            amount = balance if pct >= 100 else int(Decimal(balance) * pct / Decimal(100))
            self.db.update("sells", sell_id, {"route": snap.route, "amount_raw": str(amount)})
            user = self.wallet.pubkey
            st = snap.ps_state
            cu = self.settings.int("cu_limit_pumpswap" if snap.route == ROUTE_PUMPSWAP else "cu_limit_jupiter")
            self.risk.check_sell(exec_mode=sell["exec_mode"], tip=tip, micro_lamports=micro, cu_limit=cu,
                                 snapshot_age_ms=snap.age_ms(), wallet_lamports=accts["wallet_lamports"],
                                 amount_raw=amount,
                                 sell_disabled=bool(st and st.global_config and st.global_config.sell_disabled))
            bh, lvbh, _, _ = await self.blockhash.get()
            tip_account = self.jito.next_tip_account()
            ixs = []
            alts = []
            if snap.route == ROUTE_PUMPSWAP:
                out = st.quote_sell(amount)
                expected = out.user_out
                min_out = expected * (10_000 - slip) // 10_000
                ixs += self.pumpswap_sell_ixs(st, user, m.info.token_program, amount, min_out,
                                              amount == balance and self.settings.bool("close_ata_on_full_sell"))
            else:
                assert self.jupiter is not None
                q = await self.jupiter.quote(mint, C.WSOL_MINT, amount, slip)
                expected = q.out_amount
                min_out = q.other_amount_threshold
                sw = await self.jupiter.swap_instructions(q.raw, user)
                enc = encoded_min_out(sw.swap, amount)
                if enc is not None and enc < min_out:
                    raise PresendError(f"minOut Jupiter {enc} < {min_out}")
                ixs += sw.setup + [sw.swap] + sw.cleanup + sw.other
                alts = await self._lookup_tables(sw.lookup_tables)
            ixs.append(sol_transfer(user, tip_account, tip))
            body = list(ixs)
            tx = self._sign_unique(lambda c: compute_budget(c, micro) + body, bh, alts, tip, tip_account, cu, micro)
            t_signed = time.time()
            self.db.update("sells", sell_id, {"min_out_lamports": min_out, "expected_out_lamports": expected})
            if not self.market.is_current(snap):
                last_err = "snapshot quá cũ sau khi ký"
                continue
            sim = await self.simulator.simulate([tx], [None], min_slot=snap.slot or None)
            t_sim = time.time()
            if not sim.ok and _blockhash_problem(sim.err):
                await self.blockhash.refresh()
                last_err = "blockhash hết hạn khi mô phỏng"
                continue
            if not sim.ok and price_guard_error(sim.err, snap.route):
                raise PresendError(f"giá giảm vượt slippage bán {slip / 100:g}% lúc mô phỏng (ExceededSlippage) — "
                                   f"không bán, không gửi gì.")
            if not self.market.is_current(snap):
                last_err = "snapshot quá cũ sau mô phỏng"
                continue
            leg = Leg(0, expected, amount, 0, min_out, price_from(expected, amount, m.info.decimals), None)
            plan = Plan(snap.route, mint, m.info.decimals, m.info.token_program, [leg], snap, cu)
            sell = self.db.get("sells", sell_id)
            return await self._persist_and_send(
                purpose="SELL", order=None, sell=sell, plan=plan, txs=[tx], sim=sim, blockhash=bh, lvbh=lvbh,
                tip=tip, tip_account=tip_account, micro=micro, snap=snap,
                times={"t_signal": t_signal, "t_planned": t_signal, "t_signed": t_signed, "t_simulated": t_sim})
        raise StaleData(f"không có dữ liệu đủ mới: {last_err}")

    # ======================================================================== reports ====
    def format_buy_report(self, order: dict, sub: dict, txs: list[dict], pos: dict | None) -> str:
        dec = self._decimals(sub["mint"])
        ok = [t for t in txs if t["status"] == ST_SUCCESS]
        spent = sum(-(int(t["actual_sol_delta"] or 0) + int(t["rent_lamports"] or 0)) for t in ok)
        swap = sum(int(t["swap_lamports"] or 0) for t in ok)
        got = sum(int(t["actual_token_delta"] or 0) for t in ok)
        lines = [
            f"{'✅' if sub['status'] == ST_SUCCESS else '❌'} BUY {order['kind']} ×{order['count']} — lệnh #{order['id']}",
            f"Token: {sub['mint']}",
            f"Trạng thái xác minh: {sub['status']} (level {sub['verify_level'] or 0}, {len(ok)}/{len(txs)} giao dịch)",
            f"SOL thực chi: {sol_str(spent)} (vào pool {sol_str(swap)})",
            f"Token thực nhận: {fmt_amount(got, dec)}",
            f"Giá thực hiện TB: {price_from(swap, got, dec):.12f} SOL/token" if got else "Giá thực hiện: —",
        ]
        if order["trigger_price"]:
            lines.append(f"Giá kích hoạt/trần: {order['trigger_price']}")
        lines.append(f"TP/SL: {order['tp_pct'] or '—'}% / {order['sl_pct'] or '—'}%")
        if sub["bundle_id"]:
            lines.append(f"Bundle: {sub['bundle_id']}")
        for t in txs:
            lines.append(f"  #{t['idx'] + 1} {t['status']} {t['signature']}")
        if sub["t_signal"] and sub["t_verified"]:
            lines.append(f"Thời gian xử lý: tín hiệu→gửi {1000 * ((sub['t_sent'] or sub['t_signal']) - sub['t_signal']):.0f} ms, "
                         f"→xác nhận {1000 * (sub['t_verified'] - sub['t_signal']):.0f} ms")
        if sub["error"] and sub["status"] != ST_SUCCESS:
            lines.append(f"Lý do: {sub['error'][:300]}")
        return "\n".join(lines)

    def format_sell_report(self, sell: dict, sub: dict, txs: list[dict]) -> str:
        dec = self._decimals(sell["mint"])
        lines = [f"{'✅' if sub['status'] == ST_SUCCESS else ('⚠️' if sub['status'] == ST_HOLD else '❌')} "
                 f"SELL {sell['pct']}% ({sell['reason']}) — {sub['status']}",
                 f"Token: {sell['mint']}"]
        if sub["status"] == ST_SUCCESS:
            lines += [f"Token đã bán: {fmt_amount(int(sell['amount_raw'] or 0), dec)}",
                      f"SOL thực nhận (sau phí): {sol_str(sell['proceeds_lamports'])}",
                      f"Giá vốn phân bổ: {sol_str(sell['cost_alloc_lamports'])}",
                      f"PnL thực hiện: {sol_str(sell['pnl_lamports'])} SOL"]
            t = txs[0] if txs else None
            if t and t["exec_price"]:
                lines.append(f"Giá thực hiện: {Decimal(t['exec_price']):.12f} SOL/token")
        for t in txs:
            lines.append(f"  {t['status']} {t['signature']}")
        if sub["t_signal"] and sub["t_verified"]:
            lines.append(f"Thời gian xử lý: {1000 * (sub['t_verified'] - sub['t_signal']):.0f} ms")
        if sub["error"] and sub["status"] != ST_SUCCESS:
            lines.append(f"Lý do: {sub['error'][:300]}")
        return "\n".join(lines)

    def _decimals(self, mint: str) -> int:
        r = self.db.one("SELECT decimals FROM tokens WHERE mint=?", (mint,))
        return int(r["decimals"]) if r and r["decimals"] is not None else 6


_PUMPSWAP_GUARD = re.compile(r"0x1774|Custom['\"]?\s*:\s*6004\b|ExceededSlippage")
_JUPITER_GUARD = re.compile(r"0x1771|Custom['\"]?\s*:\s*6001\b|SlippageToleranceExceeded")


def price_guard_error(err: str | None, route: str) -> bool:
    """True when the simulation failed on the swap's own price guard (PumpSwap 6004 ExceededSlippage on
    max_quote_amount_in / min_quote_amount_out, Jupiter 6001 SlippageToleranceExceeded)."""
    if not err:
        return False
    return bool((_PUMPSWAP_GUARD if route == ROUTE_PUMPSWAP else _JUPITER_GUARD).search(err))


def _blockhash_problem(err: str | None) -> bool:
    return bool(err) and ("BlockhashNotFound" in err or "blockhash not found" in err.lower())


def fmt_amount(raw: int, decimals: int) -> str:
    return f"{Decimal(raw) / Decimal(10**decimals):,.{min(decimals, 4)}f}"
