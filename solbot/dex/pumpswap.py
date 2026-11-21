"""PumpSwap (pump AMM) — account decoding, fee schedule, exact quote math, v2 instructions.

The math is a line-by-line port of @pump-fun/pump-swap-sdk 2.1.0 (src/sdk/{buy,sell,fees,util}.ts)
and is pinned against the SDK's golden values in tests/test_pumpswap.py.

Pricing uses *effective* quote reserves (pool_quote_token_account.amount + Pool.virtual_quote_reserves,
signed). Sells are bounded by *real* quote reserves (vault - protocol_fees - creator_fees).

Trades use ``buy_v2`` / ``sell_v2`` (17 accounts, no remaining accounts). Cashback-coin pools do not
support v2; those are routed to Jupiter instead.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

from .. import constants as C

DISC_POOL = hashlib.sha256(b"account:Pool").digest()[:8]
DISC_GLOBAL_CONFIG = hashlib.sha256(b"account:GlobalConfig").digest()[:8]
DISC_FEE_CONFIG = hashlib.sha256(b"account:FeeConfig").digest()[:8]

BPS = 10_000


class DecodeError(ValueError):
    pass


class QuoteError(ValueError):
    pass


# ----------------------------------------------------------------------------------------------
# Borsh reader
# ----------------------------------------------------------------------------------------------
class Reader:
    def __init__(self, data: bytes, off: int = 0):
        self.d = data
        self.o = off

    def has(self, n: int) -> bool:
        return self.o + n <= len(self.d)

    def _take(self, n: int) -> bytes:
        if not self.has(n):
            raise DecodeError("account data too short")
        b = self.d[self.o:self.o + n]
        self.o += n
        return b

    def u8(self) -> int:
        return self._take(1)[0]

    def bool(self) -> bool:
        return self._take(1)[0] != 0

    def u16(self) -> int:
        return int.from_bytes(self._take(2), "little")

    def u32(self) -> int:
        return int.from_bytes(self._take(4), "little")

    def u64(self) -> int:
        return int.from_bytes(self._take(8), "little")

    def u128(self) -> int:
        return int.from_bytes(self._take(16), "little")

    def i128(self) -> int:
        return int.from_bytes(self._take(16), "little", signed=True)

    def pubkey(self) -> Pubkey:
        return Pubkey.from_bytes(self._take(32))

    # trailing appended fields: missing => default (docs: "read the missing trailing fields as 0/false")
    def opt(self, fn, size: int, default):
        return fn() if self.has(size) else default


# ----------------------------------------------------------------------------------------------
# Accounts
# ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PoolState:
    address: Pubkey
    pool_bump: int
    index: int
    creator: Pubkey
    base_mint: Pubkey
    quote_mint: Pubkey
    lp_mint: Pubkey
    pool_base_token_account: Pubkey
    pool_quote_token_account: Pubkey
    lp_supply: int
    coin_creator: Pubkey
    is_mayhem_mode: bool
    is_cashback_coin: bool
    virtual_quote_reserves: int  # signed i128
    creator_fee_bps: int
    can_edit_creator_fee: bool
    is_holder_reward: bool
    protocol_fees: int
    creator_fees: int

    @property
    def is_pump_pool(self) -> bool:
        return self.creator == C.pump_pool_authority(self.base_mint)

    @property
    def supports_v2(self) -> bool:
        return not self.is_cashback_coin

    @property
    def has_coin_creator(self) -> bool:
        return self.coin_creator != Pubkey.default()


def decode_pool(address: Pubkey, data: bytes) -> PoolState:
    if data[:8] != DISC_POOL:
        raise DecodeError("not a PumpSwap Pool account")
    r = Reader(data, 8)
    return PoolState(
        address=address,
        pool_bump=r.u8(),
        index=r.u16(),
        creator=r.pubkey(),
        base_mint=r.pubkey(),
        quote_mint=r.pubkey(),
        lp_mint=r.pubkey(),
        pool_base_token_account=r.pubkey(),
        pool_quote_token_account=r.pubkey(),
        lp_supply=r.u64(),
        coin_creator=r.opt(r.pubkey, 32, Pubkey.default()),
        is_mayhem_mode=r.opt(r.bool, 1, False),
        is_cashback_coin=r.opt(r.bool, 1, False),
        virtual_quote_reserves=r.opt(r.i128, 16, 0),
        creator_fee_bps=r.opt(r.u64, 8, 0),
        can_edit_creator_fee=r.opt(r.bool, 1, False),
        is_holder_reward=r.opt(r.bool, 1, False),
        protocol_fees=r.opt(r.u64, 8, 0),
        creator_fees=r.opt(r.u64, 8, 0),
    )


def encode_pool(p: PoolState) -> bytes:
    """Inverse of :func:`decode_pool` (used by the test chain emulator)."""
    out = bytearray(DISC_POOL)
    out += bytes([p.pool_bump]) + p.index.to_bytes(2, "little")
    for k in (p.creator, p.base_mint, p.quote_mint, p.lp_mint, p.pool_base_token_account,
              p.pool_quote_token_account):
        out += bytes(k)
    out += p.lp_supply.to_bytes(8, "little") + bytes(p.coin_creator)
    out += bytes([int(p.is_mayhem_mode), int(p.is_cashback_coin)])
    out += p.virtual_quote_reserves.to_bytes(16, "little", signed=True)
    out += p.creator_fee_bps.to_bytes(8, "little")
    out += bytes([int(p.can_edit_creator_fee), int(p.is_holder_reward)])
    out += p.protocol_fees.to_bytes(8, "little") + p.creator_fees.to_bytes(8, "little")
    return bytes(out)


@dataclass(frozen=True)
class GlobalConfig:
    lp_fee_bps: int
    protocol_fee_bps: int
    disable_flags: int
    coin_creator_fee_bps: int
    buyback_fee_recipients: tuple[Pubkey, ...]
    buyback_bps: int
    creator_fee_configurable: bool

    @property
    def buy_disabled(self) -> bool:
        return bool(self.disable_flags & (1 << 3))

    @property
    def sell_disabled(self) -> bool:
        return bool(self.disable_flags & (1 << 4))


def decode_global_config(data: bytes) -> GlobalConfig:
    if data[:8] != DISC_GLOBAL_CONFIG:
        raise DecodeError("not a PumpSwap GlobalConfig account")
    r = Reader(data, 8)
    r.pubkey()  # admin
    lp = r.u64()
    proto = r.u64()
    flags = r.u8()
    for _ in range(8):
        r.pubkey()  # protocol_fee_recipients
    creator_bps = r.opt(r.u64, 8, 0)
    buyback: tuple[Pubkey, ...] = ()
    buyback_bps = 0
    configurable = False
    # admin_set_coin_creator_authority, whitelist_pda, reserved_fee_recipient
    if r.has(32 * 3 + 1 + 32 * 7 + 1 + 32 * 8):
        r.pubkey(); r.pubkey(); r.pubkey()
        r.bool()  # mayhem_mode_enabled
        for _ in range(7):
            r.pubkey()  # reserved_fee_recipients
        r.bool()  # is_cashback_enabled
        buyback = tuple(r.pubkey() for _ in range(8))
        buyback_bps = r.opt(r.u64, 8, 0)
        if r.has(32 + 1 + 1):
            r.pubkey()  # boost_authority
            r.bool()  # boost_enabled
            configurable = r.bool()
    return GlobalConfig(lp, proto, flags, creator_bps, buyback, buyback_bps, configurable)


@dataclass(frozen=True)
class Fees:
    lp_bps: int
    protocol_bps: int
    creator_bps: int

    @property
    def total_bps(self) -> int:
        return self.lp_bps + self.protocol_bps + self.creator_bps


ZERO_FEES = Fees(0, 0, 0)


@dataclass(frozen=True)
class FeeTier:
    threshold: int  # market cap lamports (u128)
    fees: Fees


@dataclass(frozen=True)
class FeeConfig:
    flat_fees: Fees
    fee_tiers: tuple[FeeTier, ...]
    stable_fee_tiers: tuple[FeeTier, ...]
    exotic_flat_fees: Fees


def _read_fees(r: Reader) -> Fees:
    return Fees(r.u64(), r.u64(), r.u64())


def _read_tiers(r: Reader) -> tuple[FeeTier, ...]:
    n = r.u32()
    if n > 1000:
        raise DecodeError("fee tier vector too long")
    return tuple(FeeTier(r.u128(), _read_fees(r)) for _ in range(n))


def decode_fee_config(data: bytes) -> FeeConfig:
    if data[:8] != DISC_FEE_CONFIG:
        raise DecodeError("not a FeeConfig account")
    r = Reader(data, 8)
    r.u8()  # bump
    r.pubkey()  # admin
    flat = _read_fees(r)
    tiers = _read_tiers(r)
    stable = _read_tiers(r) if r.has(4) else ()
    exotic = _read_fees(r) if r.has(24) else ZERO_FEES
    return FeeConfig(flat, tiers, stable, exotic)


USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
SOL_LIKE_QUOTE_MINTS = (Pubkey.default(), C.WSOL_MINT, C.NATIVE_MINT_2022)


def calculate_fee_tier(tiers: tuple[FeeTier, ...], market_cap: int) -> Fees:
    if not tiers:
        raise QuoteError("Fee tiers cannot be empty")
    first = tiers[0]
    if market_cap < first.threshold:
        return first.fees
    for t in reversed(tiers):
        if market_cap >= t.threshold:
            return t.fees
    return first.fees


def pool_market_cap(base_mint_supply: int, base_reserve: int, quote_reserve: int, is_mayhem: bool) -> int:
    if base_reserve == 0:
        raise QuoteError("pool base reserve is zero")
    supply = C.PUMP_AMM_TOTAL_TOKEN_SUPPLY if is_mayhem else base_mint_supply
    return quote_reserve * supply // base_reserve


def fees_for_quote_mint(fc: FeeConfig, is_pump_pool: bool, market_cap: int, quote_mint: Pubkey) -> Fees:
    if not is_pump_pool:
        return fc.flat_fees
    if quote_mint in SOL_LIKE_QUOTE_MINTS:
        return calculate_fee_tier(fc.fee_tiers, market_cap)
    if quote_mint == USDC_MINT:
        return calculate_fee_tier(fc.stable_fee_tiers or fc.fee_tiers, market_cap)
    return fc.flat_fees if fc.exotic_flat_fees == ZERO_FEES else fc.exotic_flat_fees


@dataclass(frozen=True)
class FeeContext:
    global_config: GlobalConfig | None
    fee_config: FeeConfig | None
    is_pump_pool: bool
    quote_mint: Pubkey
    is_mayhem: bool
    base_mint_supply: int
    pool_creator_fee_bps: int


def compute_fees(ctx: FeeContext, base_reserve: int, effective_quote_reserve: int) -> Fees:
    if ctx.fee_config is not None:
        mc = pool_market_cap(ctx.base_mint_supply, base_reserve, effective_quote_reserve, ctx.is_mayhem)
        fees = fees_for_quote_mint(ctx.fee_config, ctx.is_pump_pool, mc, ctx.quote_mint)
        if ctx.global_config and ctx.global_config.creator_fee_configurable and ctx.pool_creator_fee_bps > 0:
            fees = Fees(fees.lp_bps, fees.protocol_bps, ctx.pool_creator_fee_bps)
        return fees
    if ctx.global_config is None:
        raise QuoteError("no fee configuration available")
    g = ctx.global_config
    return Fees(g.lp_fee_bps, g.protocol_fee_bps, g.coin_creator_fee_bps)


# ----------------------------------------------------------------------------------------------
# Exact math (ported from the SDK)
# ----------------------------------------------------------------------------------------------
def ceil_div(a: int, b: int) -> int:
    if b == 0:
        raise QuoteError("division by zero")
    return (a + b - 1) // b


def fee_amount(amount: int, bps: int) -> int:
    return ceil_div(amount * bps, BPS)


@dataclass(frozen=True)
class BuyCost:
    base_out: int
    quote_amount_in: int  # enters reserves (pre-fee)
    lp_fee: int
    protocol_fee: int
    creator_fee: int

    @property
    def total(self) -> int:
        """What the user pays, all pool fees included (compared with max_quote_amount_in)."""
        return self.quote_amount_in + self.lp_fee + self.protocol_fee + self.creator_fee


@dataclass(frozen=True)
class SellOut:
    base_in: int
    quote_amount_out: int  # raw, pre-fee
    lp_fee: int
    protocol_fee: int
    creator_fee: int

    @property
    def user_out(self) -> int:
        return self.quote_amount_out - self.lp_fee - self.protocol_fee - self.creator_fee


def buy_cost_for_base(base_out: int, base_reserve: int, eff_quote: int, fees: Fees,
                      has_coin_creator: bool) -> BuyCost:
    """SDK buyBaseInput: exact cost (all fees) of buying exactly ``base_out``."""
    if base_reserve <= 0 or eff_quote <= 0:
        raise QuoteError("empty reserves")
    if base_out <= 0:
        raise QuoteError("base_out must be > 0")
    if base_out >= base_reserve:
        raise QuoteError("cannot buy more base tokens than the pool reserves")
    quote_in = ceil_div(eff_quote * base_out, base_reserve - base_out)
    lp = fee_amount(quote_in, fees.lp_bps)
    proto = fee_amount(quote_in, fees.protocol_bps)
    creator = fee_amount(quote_in, fees.creator_bps) if has_coin_creator else 0
    return BuyCost(base_out, quote_in, lp, proto, creator)


def buy_base_for_quote(quote: int, base_reserve: int, eff_quote: int, fees: Fees,
                       has_coin_creator: bool) -> int:
    """SDK buyQuoteInput: base tokens obtainable for spending ``quote`` (fees included)."""
    if base_reserve <= 0 or eff_quote <= 0:
        raise QuoteError("empty reserves")
    total_bps = fees.lp_bps + fees.protocol_bps + (fees.creator_bps if has_coin_creator else 0)
    effective = quote * BPS // (BPS + total_bps)
    lp = fee_amount(effective, fees.lp_bps)
    proto = fee_amount(effective, fees.protocol_bps)
    creator = fee_amount(effective, fees.creator_bps) if has_coin_creator else 0
    tot = effective + lp + proto + creator
    if tot > quote:
        effective -= tot - quote
    inp = effective - 1
    if inp <= 0:
        return 0
    return base_reserve * inp // (eff_quote + inp)


def sell_out_for_base(base_in: int, base_reserve: int, eff_quote: int, fees: Fees,
                      has_coin_creator: bool, real_quote_reserves: int) -> SellOut:
    """SDK sellBaseInput (+ sellAmounts real-reserve check)."""
    if base_reserve <= 0 or eff_quote <= 0:
        raise QuoteError("empty reserves")
    if base_in <= 0:
        raise QuoteError("base_in must be > 0")
    raw = eff_quote * base_in // (base_reserve + base_in)
    lp = fee_amount(raw, fees.lp_bps)
    proto = fee_amount(raw, fees.protocol_bps)
    creator = fee_amount(raw, fees.creator_bps) if has_coin_creator else 0
    if real_quote_reserves < raw - lp:
        raise QuoteError("insufficient real quote reserves to cover the sell output")
    out = SellOut(base_in, raw, lp, proto, creator)
    if out.user_out < 0:
        raise QuoteError("fees exceed total output")
    return out


# ----------------------------------------------------------------------------------------------
# Market state (one consistent snapshot of pool + vaults)
# ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PumpSwapState:
    pool: PoolState
    base_reserve: int  # pool_base_token_account.amount
    quote_vault: int  # pool_quote_token_account.amount
    base_mint_supply: int
    base_decimals: int
    base_token_program: Pubkey
    quote_token_program: Pubkey
    global_config: GlobalConfig | None
    fee_config: FeeConfig | None
    slot: int = 0

    @property
    def effective_quote(self) -> int:
        return self.quote_vault + self.pool.virtual_quote_reserves

    @property
    def real_quote(self) -> int:
        return self.quote_vault - self.pool.protocol_fees - self.pool.creator_fees

    def fee_context(self) -> FeeContext:
        p = self.pool
        return FeeContext(
            global_config=self.global_config,
            fee_config=self.fee_config,
            is_pump_pool=p.is_pump_pool,
            quote_mint=p.quote_mint,
            is_mayhem=p.is_mayhem_mode,
            base_mint_supply=self.base_mint_supply,
            pool_creator_fee_bps=p.creator_fee_bps,
        )

    def fees(self) -> Fees:
        return compute_fees(self.fee_context(), self.base_reserve, self.effective_quote)

    def spot_lamports_per_raw(self) -> Fraction:
        if self.base_reserve <= 0:
            raise QuoteError("empty base reserve")
        return Fraction(self.effective_quote, self.base_reserve)

    def spot_price_sol(self) -> Decimal:
        """SOL per 1 whole token (UI units)."""
        f = self.spot_lamports_per_raw() * Fraction(10**self.base_decimals, C.LAMPORTS_PER_SOL)
        return Decimal(f.numerator) / Decimal(f.denominator)

    def market_cap_sol(self) -> Decimal:
        return self.spot_price_sol() * Decimal(self.base_mint_supply) / Decimal(10**self.base_decimals)

    def liquidity_sol(self) -> Decimal:
        return Decimal(2 * max(self.effective_quote, 0)) / Decimal(C.LAMPORTS_PER_SOL)

    # -- quotes -------------------------------------------------------------------------------
    def quote_buy_for_sol(self, lamports: int) -> BuyCost:
        fees = self.fees()
        base_out = buy_base_for_quote(lamports, self.base_reserve, self.effective_quote, fees,
                                      self.pool.has_coin_creator)
        if base_out <= 0:
            raise QuoteError("amount too small")
        cost = buy_cost_for_base(base_out, self.base_reserve, self.effective_quote, fees,
                                 self.pool.has_coin_creator)
        # rounding guard: never plan a leg whose exact cost exceeds the budget
        while cost.total > lamports and base_out > 1:
            base_out -= max(1, base_out // 1_000_000)
            cost = buy_cost_for_base(base_out, self.base_reserve, self.effective_quote, fees,
                                     self.pool.has_coin_creator)
        return cost

    def cost_for_base(self, base_out: int) -> BuyCost:
        return buy_cost_for_base(base_out, self.base_reserve, self.effective_quote, self.fees(),
                                 self.pool.has_coin_creator)

    def quote_sell(self, base_in: int) -> SellOut:
        return sell_out_for_base(base_in, self.base_reserve, self.effective_quote, self.fees(),
                                 self.pool.has_coin_creator, self.real_quote)

    # -- state transitions (v2 semantics; buyback slice cancels out of effective/real reserves) --
    def after_buy(self, cost: BuyCost) -> "PumpSwapState":
        kept = cost.protocol_fee + cost.creator_fee
        pool = replace(
            self.pool,
            virtual_quote_reserves=self.pool.virtual_quote_reserves - kept,
            protocol_fees=self.pool.protocol_fees + cost.protocol_fee,
            creator_fees=self.pool.creator_fees + cost.creator_fee,
        )
        return replace(self, pool=pool, base_reserve=self.base_reserve - cost.base_out,
                       quote_vault=self.quote_vault + cost.total)

    def after_sell(self, out: SellOut) -> "PumpSwapState":
        kept = out.protocol_fee + out.creator_fee
        pool = replace(
            self.pool,
            virtual_quote_reserves=self.pool.virtual_quote_reserves - kept,
            protocol_fees=self.pool.protocol_fees + out.protocol_fee,
            creator_fees=self.pool.creator_fees + out.creator_fee,
        )
        return replace(self, pool=pool, base_reserve=self.base_reserve + out.base_in,
                       quote_vault=self.quote_vault - out.user_out)


# ----------------------------------------------------------------------------------------------
# Instructions (v2: 17 accounts, IDL order)
# ----------------------------------------------------------------------------------------------
def _trade_v2_accounts(user: Pubkey, pool: PoolState, base_token_program: Pubkey,
                       quote_token_program: Pubkey, user_base_ata: Pubkey, user_quote_ata: Pubkey,
                       buyback_fee_recipient_ata: Pubkey) -> list[AccountMeta]:
    return [
        AccountMeta(pool.address, False, True),
        AccountMeta(user, True, True),
        AccountMeta(C.PUMP_AMM_GLOBAL_CONFIG, False, False),
        AccountMeta(pool.base_mint, False, False),
        AccountMeta(pool.quote_mint, False, False),
        AccountMeta(user_base_ata, False, True),
        AccountMeta(user_quote_ata, False, True),
        AccountMeta(pool.pool_base_token_account, False, True),
        AccountMeta(pool.pool_quote_token_account, False, True),
        AccountMeta(base_token_program, False, False),
        AccountMeta(quote_token_program, False, False),
        AccountMeta(C.SYSTEM_PROGRAM, False, False),
        AccountMeta(C.user_volume_accumulator(user), False, True),
        AccountMeta(C.PUMP_AMM_FEE_CONFIG, False, False),
        AccountMeta(buyback_fee_recipient_ata, False, True),
        AccountMeta(C.PUMP_AMM_EVENT_AUTHORITY, False, False),
        AccountMeta(C.PUMP_AMM_PROGRAM, False, False),
    ]


def build_buy_v2(user: Pubkey, pool: PoolState, base_token_program: Pubkey, quote_token_program: Pubkey,
                 user_base_ata: Pubkey, user_quote_ata: Pubkey, buyback_fee_recipient_ata: Pubkey,
                 base_amount_out: int, max_quote_amount_in: int) -> Instruction:
    if base_amount_out <= 0:
        raise QuoteError("base_amount_out must be > 0")
    data = C.IX_BUY_V2 + base_amount_out.to_bytes(8, "little") + max_quote_amount_in.to_bytes(8, "little")
    return Instruction(C.PUMP_AMM_PROGRAM, data, _trade_v2_accounts(
        user, pool, base_token_program, quote_token_program, user_base_ata, user_quote_ata,
        buyback_fee_recipient_ata))


def build_sell_v2(user: Pubkey, pool: PoolState, base_token_program: Pubkey, quote_token_program: Pubkey,
                  user_base_ata: Pubkey, user_quote_ata: Pubkey, buyback_fee_recipient_ata: Pubkey,
                  base_amount_in: int, min_quote_amount_out: int) -> Instruction:
    if base_amount_in <= 0:
        raise QuoteError("base_amount_in must be > 0")
    data = C.IX_SELL_V2 + base_amount_in.to_bytes(8, "little") + min_quote_amount_out.to_bytes(8, "little")
    return Instruction(C.PUMP_AMM_PROGRAM, data, _trade_v2_accounts(
        user, pool, base_token_program, quote_token_program, user_base_ata, user_quote_ata,
        buyback_fee_recipient_ata))


def decode_trade_v2(data: bytes) -> tuple[str, int, int]:
    """Decode buy_v2/sell_v2 instruction data -> (kind, amount, limit)."""
    disc = bytes(data[:8])
    a = int.from_bytes(data[8:16], "little")
    b = int.from_bytes(data[16:24], "little")
    if disc == C.IX_BUY_V2:
        return "buy_v2", a, b
    if disc == C.IX_SELL_V2:
        return "sell_v2", a, b
    raise DecodeError("not a PumpSwap v2 trade")


@dataclass
class PumpSwapStatic:
    """Pool addresses resolved once per token."""

    pool: PoolState
    base_decimals: int
    base_mint_supply: int
    base_token_program: Pubkey
    quote_token_program: Pubkey
    extra: dict = field(default_factory=dict)
