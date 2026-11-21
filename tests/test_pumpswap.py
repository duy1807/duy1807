"""PumpSwap math/decoding pinned against @pump-fun/pump-swap-sdk 2.1.0 goldens and the official IDL."""
import base64
import hashlib
import json
from pathlib import Path

from solders.pubkey import Pubkey

from solbot import constants as C
from solbot.dex import pumpswap as ps

FIX = Path(__file__).parent / "fixtures"


def test_instruction_discriminators_match_idl_and_anchor_hash():
    idl = json.loads((FIX / "pump_amm_idl.json").read_text())
    by_name = {i["name"]: i for i in idl["instructions"]}
    assert list(C.IX_BUY_V2) == by_name["buy_v2"]["discriminator"]
    assert list(C.IX_SELL_V2) == by_name["sell_v2"]["discriminator"]
    assert C.IX_BUY_V2 == hashlib.sha256(b"global:buy_v2").digest()[:8]
    assert C.IX_SELL_V2 == hashlib.sha256(b"global:sell_v2").digest()[:8]
    accs = {a["name"]: a["discriminator"] for a in idl["accounts"]}
    assert list(ps.DISC_POOL) == accs["Pool"]
    assert list(ps.DISC_GLOBAL_CONFIG) == accs["GlobalConfig"]
    assert list(ps.DISC_FEE_CONFIG) == accs["FeeConfig"]


def test_v2_account_order_matches_idl():
    idl = json.loads((FIX / "pump_amm_idl.json").read_text())
    ix = [i for i in idl["instructions"] if i["name"] == "buy_v2"][0]
    user = Pubkey.new_unique()
    pool = _pool()
    built = ps.build_buy_v2(user, pool, C.TOKEN_PROGRAM, C.TOKEN_PROGRAM, Pubkey.new_unique(),
                            Pubkey.new_unique(), Pubkey.new_unique(), 10, 20)
    assert len(built.accounts) == len(ix["accounts"]) == 17
    for meta, spec in zip(built.accounts, ix["accounts"]):
        assert meta.is_writable == bool(spec.get("writable")), spec["name"]
        assert meta.is_signer == bool(spec.get("signer")), spec["name"]
        if spec.get("address"):
            assert str(meta.pubkey) == spec["address"], spec["name"]
    kind, a, b = ps.decode_trade_v2(bytes(built.data))
    assert (kind, a, b) == ("buy_v2", 10, 20)


def test_known_pdas():
    # documented addresses (docs/instructions/PUMP_SWAP_TRADE_V2.md)
    assert str(C.PUMP_AMM_GLOBAL_CONFIG) == "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw"
    assert str(C.PUMP_AMM_FEE_CONFIG) == "5PHirr8joyTMp9JMm6nW7hNDVyEYdkzDqazxPD7RaTjx"


def _mainnet_fee_config() -> ps.FeeConfig:
    raw = base64.b64decode((FIX / "fee_config_mainnet.b64").read_text().strip())
    return ps.decode_fee_config(raw)


def test_decode_mainnet_fee_config():
    fc = _mainnet_fee_config()
    assert fc.flat_fees == ps.Fees(25, 5, 0)
    assert len(fc.fee_tiers) == 25 and len(fc.stable_fee_tiers) == 25
    assert fc.fee_tiers[0] == ps.FeeTier(0, ps.Fees(2, 93, 30))
    assert fc.fee_tiers[1] == ps.FeeTier(420_000_000_000, ps.Fees(20, 5, 95))
    assert fc.fee_tiers[-1] == ps.FeeTier(98_240_000_000_000, ps.Fees(20, 5, 5))
    assert fc.exotic_flat_fees == ps.ZERO_FEES


def test_buy_base_input_golden_flat_fees():
    # buy.spec.ts "should compute quote + fees + slippage correctly": lp 30, protocol 20
    fees = ps.Fees(30, 20, 0)
    cost = ps.buy_cost_for_base(10_000, 1_000_000, 2_000_000, fees, has_coin_creator=False)
    assert cost.total == 20305
    assert cost.total * 101 // 100 == 20508  # SDK maxQuote with 1% slippage


def test_sell_base_input_golden_flat_fees():
    fees = ps.Fees(30, 20, 0)
    out = ps.sell_out_for_base(50_000, 1_000_000, 2_000_000, fees, False, 2_000_000)
    assert out.user_out == 94761
    assert out.user_out * 99 // 100 == 93813


def _ctx(fc, supply=10**15, pump=True):
    return ps.FeeContext(None, fc, pump, C.WSOL_MINT, False, supply, 0)


def test_sell_mainnet_fee_tier_goldens():
    fc = _mainnet_fee_config()
    # sell.spec.ts "SOL pools keep their pre-change amounts" #1
    B, Q, base = 500_000_000_000_000, 50_000_000_000, 1_000_000_000_000
    fees = ps.compute_fees(_ctx(fc), B, Q)
    assert fees == ps.Fees(2, 93, 30)
    out = ps.sell_out_for_base(base, B, Q, fees, True, Q)
    assert out.quote_amount_out == 99_800_399
    assert out.user_out == 98_552_892
    # #2: coinCreator default, virtual reserves 3e10, slippage 5%
    Q2 = 1_000_000_000_000 + 30_000_000_000
    fees2 = ps.compute_fees(_ctx(fc), B, Q2)
    out2 = ps.sell_out_for_base(10_000_000_000_000, B, Q2, fees2, False, 1_000_000_000_000)
    assert out2.quote_amount_out == 20_196_078_431
    assert out2.user_out == 20_145_588_234
    assert out2.user_out * 95 // 100 == 19_138_308_822


def test_buy_quote_roundtrip_never_exceeds_budget():
    fc = _mainnet_fee_config()
    st = _state(fc)
    for lamports in (10_000, 1_000_000, 100_000_000, 5_000_000_000):
        cost = st.quote_buy_for_sol(lamports)
        assert 0 < cost.total <= lamports
        assert cost.total >= lamports * 0.98  # nearly all of the budget is used


def test_sequential_legs_raise_price():
    fc = _mainnet_fee_config()
    st = _state(fc)
    prices = []
    for _ in range(5):
        cost = st.quote_buy_for_sol(1_000_000_000)
        prices.append(cost.total / cost.base_out)
        st = st.after_buy(cost)
    assert prices == sorted(prices) and prices[0] < prices[-1]
    # effective reserves grew by exactly quote_in + lp each leg; fee buckets accrued
    assert st.pool.protocol_fees > 0 and st.pool.virtual_quote_reserves < 0


def test_pool_decode_roundtrip_and_short_layout():
    p = _pool(virtual=-123, pf=7, cf=9)
    data = ps.encode_pool(p)
    assert ps.decode_pool(p.address, data) == p
    short = data[:8 + 1 + 2 + 32 * 6 + 8 + 32 + 2]  # pre-virtual-reserves layout
    d = ps.decode_pool(p.address, short)
    assert d.virtual_quote_reserves == 0 and d.protocol_fees == 0


def _pool(virtual=0, pf=0, cf=0, mint=None):
    mint = mint or Pubkey.new_unique()
    return ps.PoolState(
        address=Pubkey.new_unique(), pool_bump=255, index=0, creator=C.pump_pool_authority(mint),
        base_mint=mint, quote_mint=C.WSOL_MINT, lp_mint=Pubkey.new_unique(),
        pool_base_token_account=Pubkey.new_unique(), pool_quote_token_account=Pubkey.new_unique(),
        lp_supply=1, coin_creator=Pubkey.new_unique(), is_mayhem_mode=False, is_cashback_coin=False,
        virtual_quote_reserves=virtual, creator_fee_bps=0, can_edit_creator_fee=False,
        is_holder_reward=False, protocol_fees=pf, creator_fees=cf,
    )


def _state(fc):
    return ps.PumpSwapState(
        pool=_pool(), base_reserve=200_000_000_000_000, quote_vault=85_000_000_000,
        base_mint_supply=10**15, base_decimals=6, base_token_program=C.TOKEN_PROGRAM,
        quote_token_program=C.TOKEN_PROGRAM, global_config=None, fee_config=fc,
    )
