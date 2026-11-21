"""Market Data Engine.

* Resolves a mint to a route (PumpSwap canonical pool via PDA / getProgramAccounts, else Jupiter).
* Keeps one consistent snapshot per watched token:
  - PumpSwap: Pool + base vault + quote vault, streamed from Yellowstone gRPC (grouped by
    transaction signature so the three accounts are always from the same state), with an HTTP
    polling fallback (getMultipleAccounts, single context slot) when gRPC is not configured/down.
  - Jupiter: periodic probe quotes.
* ``Snapshot.age_ms`` = time since the state was last *confirmed current* (an account update or a
  stream heartbeat while the stream is healthy). Trading code only accepts snapshots younger than
  ``Config.snapshot_max_age_ms`` (<= 600 ms).
* Maintains a blockhash cache.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Callable

from solders.pubkey import Pubkey

from . import constants as C
from .config import Config
from .db import Database
from .dex import pumpswap as ps
from .dex.jupiter import JupiterClient, JupiterQuote
from .rpc import RpcPool, mint_supply_decimals, token_account_amount

log = logging.getLogger("solbot.market")

ROUTE_PUMPSWAP = "pumpswap"
ROUTE_JUPITER = "jupiter"


class MarketError(RuntimeError):
    pass


class StaleData(MarketError):
    pass


def now_ms() -> float:
    return time.monotonic() * 1000


@dataclass
class Snapshot:
    mint: Pubkey
    route: str
    version: int
    slot: int
    price_sol: Decimal  # spot SOL per 1 whole token
    received_ms: float  # monotonic ms when this state arrived
    source: str  # grpc | poll | rpc | jupiter
    ps_state: ps.PumpSwapState | None = None
    jup_quote: JupiterQuote | None = None
    confirmed_ms: float = 0.0  # last time we knew this state was still current
    superseded: bool = False

    def age_ms(self, at_ms: float | None = None) -> float:
        return (at_ms if at_ms is not None else now_ms()) - max(self.received_ms, self.confirmed_ms)


@dataclass
class TokenInfo:
    mint: Pubkey
    decimals: int
    supply: int
    token_program: Pubkey
    name: str = ""
    symbol: str = ""


@dataclass
class TokenMarket:
    info: TokenInfo
    route: str
    pool: ps.PoolState | None = None
    snapshot: Snapshot | None = None
    version: int = 0
    # pumpswap account state: role -> (slot, data/amount)
    acc_pool: tuple[int, bytes] | None = None
    acc_base: tuple[int, int] | None = None
    acc_quote: tuple[int, int] | None = None
    pending: dict[bytes, dict] = field(default_factory=dict)
    watchers: set[str] = field(default_factory=set)
    poll_task: asyncio.Task | None = None
    synced: bool = False  # base state fetched after (re)subscription

    @property
    def mint(self) -> Pubkey:
        return self.info.mint


Listener = Callable[[Snapshot], None]


class BlockhashCache:
    def __init__(self, rpc: RpcPool, max_age_ms: int = 20_000, refresh_ms: int = 1_000):
        self.rpc = rpc
        self.max_age_ms = max_age_ms
        self.refresh_ms = refresh_ms
        self.value: tuple[str, int, int] | None = None  # (blockhash, last_valid_block_height, slot)
        self.fetched_ms = 0.0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def refresh(self) -> tuple[str, int, int]:
        async with self._lock:
            v = await self.rpc.get_latest_blockhash("confirmed")
            self.value = v
            self.fetched_ms = now_ms()
            return v

    async def get(self) -> tuple[str, int, int, float]:
        if self.value is None or now_ms() - self.fetched_ms > self.max_age_ms:
            await self.refresh()
        assert self.value
        return (*self.value, now_ms() - self.fetched_ms)

    def start(self) -> None:
        async def loop():
            while True:
                try:
                    await self.refresh()
                except Exception as e:  # keep running; get() will retry synchronously
                    log.warning("blockhash refresh failed: %s", e)
                await asyncio.sleep(self.refresh_ms / 1000)

        self._task = asyncio.create_task(loop(), name="blockhash")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()


class MarketEngine:
    def __init__(self, config: Config, rpc: RpcPool, db: Database, jupiter: JupiterClient | None = None):
        self.config = config
        self.rpc = rpc
        self.db = db
        self.jupiter = jupiter
        self.markets: dict[Pubkey, TokenMarket] = {}
        self.listeners: list[Listener] = []
        self.global_config: ps.GlobalConfig | None = None
        self.fee_config: ps.FeeConfig | None = None
        self._cfg_task: asyncio.Task | None = None
        self.geyser = None  # set by app when GRPC_ENDPOINT configured
        self._resolve_locks: dict[Pubkey, asyncio.Lock] = {}
        self.jup_probe_lamports = 10_000_000  # 0.01 SOL

    # ------------------------------------------------------------------ program configs --
    async def refresh_configs(self) -> None:
        _, accs = await self.rpc.get_multiple_accounts([C.PUMP_AMM_GLOBAL_CONFIG, C.PUMP_AMM_FEE_CONFIG],
                                                        "confirmed")
        gc, fc = accs
        if gc:
            self.global_config = ps.decode_global_config(gc.data)
        if fc:
            self.fee_config = ps.decode_fee_config(fc.data)

    def start(self) -> None:
        async def loop():
            while True:
                await asyncio.sleep(60)
                try:
                    await self.refresh_configs()
                except Exception as e:
                    log.warning("config refresh failed: %s", e)

        self._cfg_task = asyncio.create_task(loop(), name="pump-configs")

    async def stop(self) -> None:
        if self._cfg_task:
            self._cfg_task.cancel()
        for m in self.markets.values():
            if m.poll_task:
                m.poll_task.cancel()

    def add_listener(self, fn: Listener) -> None:
        self.listeners.append(fn)

    # ------------------------------------------------------------------------ resolve --
    async def resolve(self, mint: Pubkey, route_pref: str = "auto") -> TokenMarket:
        m = self.markets.get(mint)
        if m and (route_pref == "auto" or m.route == route_pref):
            return m
        lock = self._resolve_locks.setdefault(mint, asyncio.Lock())
        async with lock:
            m = self.markets.get(mint)
            if m and (route_pref == "auto" or m.route == route_pref):
                return m
            if self.global_config is None or self.fee_config is None:
                await self.refresh_configs()
            info = await self._token_info(mint)
            pool = None
            if route_pref in ("auto", ROUTE_PUMPSWAP):
                pool = await self._find_pumpswap_pool(mint)
            route = ROUTE_PUMPSWAP if pool is not None else ROUTE_JUPITER
            if route_pref == ROUTE_PUMPSWAP and pool is None:
                raise MarketError("Không tìm thấy pool PumpSwap hỗ trợ v2 cho token này")
            if route == ROUTE_JUPITER and self.jupiter is None:
                raise MarketError("Token không có pool PumpSwap và Jupiter chưa được cấu hình")
            m = TokenMarket(info=info, route=route, pool=pool)
            self.markets[mint] = m
            self._persist(m)
            return m

    def _persist(self, m: TokenMarket) -> None:
        i = m.info
        self.db.execute(
            "INSERT INTO tokens(mint,symbol,name,decimals,token_program,supply,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(mint) DO UPDATE SET symbol=excluded.symbol,name=excluded.name,decimals=excluded.decimals,"
            "token_program=excluded.token_program,supply=excluded.supply,updated_at=excluded.updated_at",
            (str(i.mint), i.symbol, i.name, i.decimals, str(i.token_program), str(i.supply), time.time()),
        )
        if m.pool:
            p = m.pool
            self.db.execute(
                "INSERT INTO pools(address,mint,dex,base_vault,quote_vault,quote_mint,creator,coin_creator,"
                "is_pump_pool,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(address) DO UPDATE SET "
                "updated_at=excluded.updated_at",
                (str(p.address), str(i.mint), ROUTE_PUMPSWAP, str(p.pool_base_token_account),
                 str(p.pool_quote_token_account), str(p.quote_mint), str(p.creator), str(p.coin_creator),
                 int(p.is_pump_pool), time.time()),
            )

    async def _token_info(self, mint: Pubkey) -> TokenInfo:
        _, (acc, meta) = await self.rpc.get_multiple_accounts([mint, C.metadata_pda(mint)], "confirmed")
        if acc is None:
            raise MarketError("Mint không tồn tại")
        if acc.owner not in (C.TOKEN_PROGRAM, C.TOKEN_2022_PROGRAM):
            raise MarketError("Địa chỉ không phải SPL mint")
        supply, decimals = mint_supply_decimals(acc.data)
        name, symbol = "", ""
        if meta is not None:
            name, symbol = _decode_metaplex_name(meta.data)
        if not name and acc.owner == C.TOKEN_2022_PROGRAM:
            name, symbol = _decode_token2022_metadata(acc.data)
        return TokenInfo(mint, decimals, supply, acc.owner, name, symbol)

    async def _find_pumpswap_pool(self, mint: Pubkey) -> ps.PoolState | None:
        canonical = C.canonical_pump_pool(mint)
        acc = await self.rpc.get_account(canonical, "confirmed")
        candidates: list[ps.PoolState] = []
        if acc is not None and acc.owner == C.PUMP_AMM_PROGRAM:
            candidates.append(ps.decode_pool(canonical, acc.data))
        else:
            # non-canonical pools: Pool.base_mint at offset 43, quote_mint at 75
            try:
                accs = await self.rpc.get_program_accounts(C.PUMP_AMM_PROGRAM, [
                    {"memcmp": {"offset": 0, "bytes": _b58(ps.DISC_POOL)}},
                    {"memcmp": {"offset": 43, "bytes": str(mint)}},
                    {"memcmp": {"offset": 75, "bytes": str(C.WSOL_MINT)}},
                ])
                candidates = [ps.decode_pool(a.pubkey, a.data) for a in accs]
            except Exception as e:
                log.info("getProgramAccounts unavailable for pool discovery: %s", e)
        good = [p for p in candidates if p.quote_mint == C.WSOL_MINT and p.supports_v2]
        if not good:
            return None
        if len(good) == 1:
            return good[0]
        # choose the deepest pool
        _, vaults = await self.rpc.get_multiple_accounts([p.pool_quote_token_account for p in good], "confirmed")
        best = max(zip(good, vaults), key=lambda t: token_account_amount(t[1].data) if t[1] else -1)
        return best[0]

    # ------------------------------------------------------------------------ watching --
    async def watch(self, mint: Pubkey, reason: str, route_pref: str = "auto") -> TokenMarket:
        m = await self.resolve(mint, route_pref)
        first = not m.watchers
        m.watchers.add(reason)
        if first:
            await self._start_feed(m)
        return m

    def unwatch(self, mint: Pubkey, reason: str) -> None:
        m = self.markets.get(mint)
        if not m:
            return
        m.watchers.discard(reason)
        if not m.watchers:
            if m.poll_task:
                m.poll_task.cancel()
                m.poll_task = None
            if self.geyser and m.route == ROUTE_PUMPSWAP:
                self.geyser.remove_market(m)

    async def _start_feed(self, m: TokenMarket) -> None:
        if m.route == ROUTE_PUMPSWAP:
            if self.geyser is not None:
                self.geyser.add_market(m)
                await self.fetch_now(m)  # base state while the stream warms up
            else:
                m.poll_task = asyncio.create_task(self._poll_loop(m), name=f"poll-{str(m.mint)[:6]}")
        else:
            m.poll_task = asyncio.create_task(self._jup_loop(m), name=f"jup-{str(m.mint)[:6]}")

    async def _poll_loop(self, m: TokenMarket) -> None:
        interval = self.config.poll_interval_ms / 1000
        while True:
            try:
                await self.fetch_now(m)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("poll %s failed: %s", m.mint, e)
            await asyncio.sleep(interval)

    async def _jup_loop(self, m: TokenMarket) -> None:
        interval = self.config.jupiter_price_poll_ms / 1000
        while True:
            try:
                await self.fetch_now(m)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("jupiter probe %s failed: %s", m.mint, e)
            await asyncio.sleep(interval)

    # ----------------------------------------------------------------------- fetching --
    async def fetch_now(self, m: TokenMarket) -> Snapshot:
        """Fetch a fresh consistent state over HTTP RPC (single context slot) and publish it."""
        t0 = now_ms()
        if m.route == ROUTE_PUMPSWAP:
            assert m.pool is not None
            slot, (pacc, bacc, qacc) = await self.rpc.get_multiple_accounts(
                [m.pool.address, m.pool.pool_base_token_account, m.pool.pool_quote_token_account], "processed")
            if pacc is None or bacc is None or qacc is None:
                raise MarketError("pool accounts missing")
            self._apply_pool(m, slot, pacc.data)
            self._apply_vault(m, "base", slot, token_account_amount(bacc.data))
            self._apply_vault(m, "quote", slot, token_account_amount(qacc.data))
            m.synced = True
            snap = self._publish(m, slot, "rpc" if self.geyser else "poll", received_ms=t0)
            return snap
        assert self.jupiter is not None
        q = await self.jupiter.quote(C.WSOL_MINT, m.mint, self.jup_probe_lamports, 50)
        if q.out_amount <= 0:
            raise MarketError("Jupiter returned zero output")
        lamports_per_raw = Fraction(q.in_amount, q.out_amount)
        price = _lamports_per_raw_to_sol(lamports_per_raw, m.info.decimals)
        m.version += 1
        snap = Snapshot(m.mint, ROUTE_JUPITER, m.version, int(q.context_slot or 0), price, t0, "jupiter",
                        jup_quote=q, confirmed_ms=t0)
        self._set_snapshot(m, snap)
        return snap

    def _apply_pool(self, m: TokenMarket, slot: int, data: bytes) -> None:
        if m.acc_pool is None or slot >= m.acc_pool[0]:
            m.acc_pool = (slot, data)

    def _apply_vault(self, m: TokenMarket, role: str, slot: int, amount: int) -> None:
        cur = m.acc_base if role == "base" else m.acc_quote
        if cur is None or slot >= cur[0]:
            if role == "base":
                m.acc_base = (slot, amount)
            else:
                m.acc_quote = (slot, amount)

    def _publish(self, m: TokenMarket, slot: int, source: str, received_ms: float | None = None) -> Snapshot:
        assert m.acc_pool and m.acc_base and m.acc_quote and m.pool
        pool = ps.decode_pool(m.pool.address, m.acc_pool[1])
        m.pool = pool
        st = ps.PumpSwapState(
            pool=pool, base_reserve=m.acc_base[1], quote_vault=m.acc_quote[1],
            base_mint_supply=m.info.supply, base_decimals=m.info.decimals,
            base_token_program=m.info.token_program, quote_token_program=C.TOKEN_PROGRAM,
            global_config=self.global_config, fee_config=self.fee_config,
            slot=max(m.acc_pool[0], m.acc_base[0], m.acc_quote[0]),
        )
        t = received_ms if received_ms is not None else now_ms()
        m.version += 1
        snap = Snapshot(m.mint, ROUTE_PUMPSWAP, m.version, st.slot, st.spot_price_sol(), t, source,
                        ps_state=st, confirmed_ms=t)
        self._set_snapshot(m, snap)
        return snap

    def _set_snapshot(self, m: TokenMarket, snap: Snapshot) -> None:
        if m.snapshot is not None:
            m.snapshot.superseded = True
        m.snapshot = snap
        self.rpc.observe_slot(snap.slot)
        for fn in list(self.listeners):
            try:
                fn(snap)
            except Exception:
                log.exception("market listener failed")

    # ------------------------------------------------------------------------ access --
    def current(self, mint: Pubkey) -> Snapshot | None:
        m = self.markets.get(mint)
        return m.snapshot if m else None

    async def fresh_snapshot(self, mint: Pubkey, max_age_ms: float | None = None) -> Snapshot:
        """Return a snapshot no older than ``max_age_ms``; fetch one over RPC if needed."""
        max_age = self.config.snapshot_max_age_ms if max_age_ms is None else max_age_ms
        m = self.markets.get(mint) or await self.resolve(mint)
        s = m.snapshot
        if s is not None and s.age_ms() <= max_age * 0.5:
            return s
        s = await self.fetch_now(m)
        if s.age_ms() > max_age:
            raise StaleData(f"snapshot age {s.age_ms():.0f} ms > {max_age} ms")
        return s

    def is_current(self, snap: Snapshot, max_age_ms: float | None = None) -> bool:
        max_age = self.config.snapshot_max_age_ms if max_age_ms is None else max_age_ms
        return snap.age_ms() <= max_age

    # ------------------------------------------------------------- geyser callbacks --
    def on_heartbeat(self, slot: int) -> None:
        """Stream healthy at ``slot``: every synced gRPC market without pending partial updates is current."""
        self.rpc.observe_slot(slot)
        t = now_ms()
        for m in self.markets.values():
            if m.route == ROUTE_PUMPSWAP and m.watchers and m.synced and not m.pending and m.snapshot:
                if m.snapshot.source in ("grpc", "rpc"):
                    m.snapshot.confirmed_ms = t

    def on_stream_down(self) -> None:
        for m in self.markets.values():
            if m.route == ROUTE_PUMPSWAP:
                m.synced = False
                m.pending.clear()

    def on_account_update(self, m: TokenMarket, role: str, slot: int, data: bytes, sig: bytes | None) -> None:
        key = sig or b"nosig:%d" % slot
        group = m.pending.setdefault(key, {"t": now_ms(), "slot": slot})
        group[role] = data
        if all(r in group for r in ("pool", "base", "quote")):
            self._commit_group(m, key)
        else:
            asyncio.get_event_loop().call_later(0.025, self._commit_group, m, key)

    def _commit_group(self, m: TokenMarket, key: bytes) -> None:
        group = m.pending.pop(key, None)
        if group is None:
            return
        slot = group["slot"]
        if "pool" in group:
            self._apply_pool(m, slot, group["pool"])
        if "base" in group:
            self._apply_vault(m, "base", slot, token_account_amount(group["base"]))
        if "quote" in group:
            self._apply_vault(m, "quote", slot, token_account_amount(group["quote"]))
        if m.synced and m.acc_pool and m.acc_base and m.acc_quote:
            self._publish(m, slot, "grpc")


def _lamports_per_raw_to_sol(f: Fraction, decimals: int) -> Decimal:
    g = f * Fraction(10**decimals, C.LAMPORTS_PER_SOL)
    return Decimal(g.numerator) / Decimal(g.denominator)


def _b58(b: bytes) -> str:
    import base58

    return base58.b58encode(b).decode()


def _read_borsh_string(data: bytes, off: int) -> tuple[str, int]:
    n = int.from_bytes(data[off:off + 4], "little")
    if n > 1000:
        raise ValueError("string too long")
    s = data[off + 4:off + 4 + n].decode("utf-8", "replace").rstrip("\x00").strip()
    return s, off + 4 + n


def _decode_metaplex_name(data: bytes) -> tuple[str, str]:
    try:
        off = 1 + 32 + 32
        name, off = _read_borsh_string(data, off)
        symbol, off = _read_borsh_string(data, off)
        return name, symbol
    except Exception:
        return "", ""


def _decode_token2022_metadata(data: bytes) -> tuple[str, str]:
    """Token-2022 TokenMetadata extension (type 19) inside the mint account TLV area."""
    try:
        off = 166  # 165-byte base (padded) + account type byte
        while off + 4 <= len(data):
            typ = int.from_bytes(data[off:off + 2], "little")
            ln = int.from_bytes(data[off + 2:off + 4], "little")
            val = data[off + 4:off + 4 + ln]
            if typ == 19:
                p = 64
                name, p = _read_borsh_string(val, p)
                symbol, p = _read_borsh_string(val, p)
                return name, symbol
            if typ == 0 and ln == 0:
                break
            off += 4 + ln
    except Exception:
        pass
    return "", ""
