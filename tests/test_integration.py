"""End-to-end tests of the 9 BUY forms, SELL, TP/SL, failures and restart against the fake chain.

The fake chain executes the real signed transactions produced by the bot, so these tests exercise
instruction encoding, sequential bundle pricing, on-chain price guards, Jito handling,
two-RPC verification, positions/PnL and recovery together.
"""
import asyncio
from decimal import Decimal

import pytest

from solbot.orders import OrderParams
from solbot.settings import MODE_NO_SEND

from .conftest import wait_for

FINAL = ("FILLED", "FAILED_SAFE", "HOLD", "DRY_RUN", "CANCELLED")


def params(count, sol="0.1", slip="5", tp="0", sl="0", dip=None, limit=None):
    return OrderParams(sol_per_buy=Decimal(sol), count=count, slippage_pct=Decimal(slip), tp_pct=Decimal(tp),
                       sl_pct=Decimal(sl), dip_pct=Decimal(dip) if dip else None,
                       limit_price=Decimal(limit) if limit else None)


async def final_order(app, oid, timeout=5.0):
    return await wait_for(lambda: (lambda o: o if o["status"] in FINAL else None)(app.db.get("orders", oid)),
                          timeout=timeout)


def legs(app, sub_id):
    return app.db.query("SELECT * FROM sub_txs WHERE submission_id=? ORDER BY idx", (sub_id,))


# ------------------------------------------------------------------------------------------------
# 1) BUY NGAY ×1/×3/×5
# ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("count", [1, 3, 5])
async def test_buy_now(harness, count):
    app = await harness.start()
    ch = harness.chain
    bal0 = ch.accounts[harness.kp.pubkey()].lamports
    order, res = await app.orders.create_and_wait("NOW", str(ch.mint), params(count), None)
    assert res.status == "SENT"
    o = await final_order(app, order["id"])
    assert o["status"] == "FILLED"
    sub = app.db.get("submissions", o["submission_id"])
    assert sub["status"] == "SUCCESS" and sub["verify_level"] == 2
    assert sub["sim_method"] == ("simulateTransaction" if count == 1 else "simulateBundle")
    assert sub["bundle_id"]
    ls = legs(app, sub["id"])
    assert len(ls) == count and all(l["status"] == "SUCCESS" for l in ls)
    # sequential pricing: each leg pays more per token than the previous one
    prices = [Decimal(l["exec_price"]) for l in ls]
    assert prices == sorted(prices)
    # planned == actual on-chain
    for l in ls:
        assert int(l["actual_token_delta"]) == int(l["planned_out_raw"])
        assert l["swap_lamports"] <= l["max_in_lamports"]
    # tip paid exactly once, in the last tx
    assert [l["tip_paid_lamports"] for l in ls] == [0] * (count - 1) + [sub["tip_lamports"]]
    pos = app.positions.open_position(str(ch.mint))
    assert int(pos["token_raw"]) == ch.token_balance(harness.kp.pubkey())
    spent = bal0 - ch.accounts[harness.kp.pubkey()].lamports
    assert pos["cost_lamports"] + pos["rent_lamports"] == spent  # every lamport accounted for
    jito = [c for c in ch.jito_calls if c["method"] in ("sendTransaction", "sendBundle")]
    assert len(jito) == 1 and jito[0]["method"] == ("sendTransaction" if count == 1 else "sendBundle")


# ------------------------------------------------------------------------------------------------
# 2) FIXED DIP ×1/×3/×5 and 3) LIMIT ×1/×3/×5
# ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["DIP", "LIMIT"])
@pytest.mark.parametrize("count", [1, 3, 5])
async def test_dip_and_limit(harness, kind, count):
    app = await harness.start()
    ch = harness.chain
    p0 = ch.spot_price()
    if kind == "DIP":
        p = params(count, tp="25", sl="10", dip="10")
    else:
        p = params(count, tp="25", sl="10", limit=str((p0 * Decimal("0.9")).quantize(Decimal("1e-15"))))
    order = await app.orders.create(kind, str(ch.mint), p, None)
    assert order["status"] == "ARMED"
    trig = Decimal(order["trigger_price"])
    if kind == "DIP":
        assert Decimal(order["reference_price"]) == p0
        assert trig == (p0 * Decimal("0.9")).normalize()
    await asyncio.sleep(0.1)
    assert app.db.get("orders", order["id"])["status"] == "ARMED"  # not yet
    ch.move_price_to(0.85)  # price falls ~15%
    o = await final_order(app, order["id"])
    assert o["status"] == "FILLED", o
    sub = app.db.get("submissions", o["submission_id"])
    ls = legs(app, sub["id"])
    assert len(ls) == count
    for l in ls:
        # execution price (all pool fees included) never above the locked trigger price
        assert Decimal(l["exec_price"]) <= trig
        assert Decimal(l["limit_price"]) == trig
        assert l["max_in_lamports"] <= int(Decimal(l["planned_out_raw"]) * trig * 10**9 / 10**6)
    pos = app.positions.open_position(str(ch.mint))
    assert pos["tpsl_enabled"] == 1 and pos["tp_pct"] == "25" and pos["sl_pct"] == "10"
    # one-shot: further dips never re-trigger the order
    q = "SELECT COUNT(*) c FROM submissions WHERE purpose='BUY' AND order_id=?"
    assert app.db.one(q, (order["id"],))["c"] == 1
    ch.move_price_to(0.9)
    await asyncio.sleep(0.2)
    assert app.db.one(q, (order["id"],))["c"] == 1
    assert app.db.get("orders", order["id"])["triggered_at"] == o["triggered_at"]


async def test_dip_waits_when_legs_would_exceed_ceiling(harness):
    """Spot touches the trigger but fees+impact would put executions above it -> no send, stays ARMED."""
    app = await harness.start()
    ch = harness.chain
    order = await app.orders.create("DIP", str(ch.mint), params(5, sol="0.3", dip="10"), None)
    trig = Decimal(order["trigger_price"])
    # walk the price down in tiny steps until spot is just below the trigger
    while ch.spot_price() > trig:
        st = ch.pool_state()
        ch.external_trade("sell", base=st.base_reserve // 4000)
    assert ch.spot_price() > trig * Decimal("0.995")
    await asyncio.sleep(0.2)
    o = app.db.get("orders", order["id"])
    assert o["status"] == "ARMED" and o["submission_id"] is None
    assert "trần" in (o["wait_note"] or "")
    ch.move_price_to(0.9, step=400)
    o = await final_order(app, order["id"])
    assert o["status"] == "FILLED", (o["status_reason"], o["wait_note"])
    for l in legs(app, o["submission_id"]):
        assert Decimal(l["exec_price"]) <= trig


async def test_frontrun_cannot_break_price_ceiling(harness):
    """Someone pumps the pool between our plan and landing: the on-chain max_in guard rejects the
    bundle, nothing executes above the ceiling, the order becomes FAILED_SAFE and is never re-sent."""
    app = await harness.start(verify_deadline_s=3.0)
    ch = harness.chain
    order = await app.orders.create("LIMIT", str(ch.mint),
                                    params(3, slip="100", limit=str(ch.spot_price() * Decimal("0.95"))), None)
    pump = {"done": False}

    def frontrun():
        if not pump["done"]:
            pump["done"] = True
            ch.move_price_to(1.10)

    ch.before_land_hook = frontrun
    ch.move_price_to(0.9)
    await wait_for(lambda: app.db.get("orders", order["id"])["status"] == "SENT", timeout=5)
    assert ch.token_balance(harness.kp.pubkey()) == 0  # nothing landed
    ch.advance(300)  # blockhash expires
    o = await final_order(app, order["id"])
    assert o["status"] == "FAILED_SAFE"
    sub = app.db.get("submissions", o["submission_id"])
    assert sub["status"] == "FAILED_SAFE" and "expired" in sub["error"]
    ch.before_land_hook = None
    ch.move_price_to(0.7)
    await asyncio.sleep(0.2)
    assert app.db.one("SELECT COUNT(*) c FROM submissions WHERE purpose='BUY'")["c"] == 1  # never re-sent


# ------------------------------------------------------------------------------------------------
# NO_SEND acceptance for all 9 forms
# ------------------------------------------------------------------------------------------------
async def test_no_send_all_nine_forms(harness):
    app = await harness.start(live=False)
    assert app.settings.mode == MODE_NO_SEND
    ch = harness.chain
    bal0 = ch.accounts[harness.kp.pubkey()].lamports
    ids = []
    for count in (1, 3, 5):
        o, res = await app.orders.create_and_wait("NOW", str(ch.mint), params(count), None)
        assert res.status == "DRY_RUN", res
        ids.append(o["id"])
        ids.append((await app.orders.create("DIP", str(ch.mint), params(count, dip="10"), None))["id"])
        ids.append((await app.orders.create("LIMIT", str(ch.mint),
                                            params(count, limit=str(ch.spot_price() * Decimal("0.92"))), None))["id"])
    ch.move_price_to(0.85)
    for oid in ids:
        o = await final_order(app, oid)
        assert o["status"] == "DRY_RUN", o
        sub = app.db.get("submissions", o["submission_id"])
        assert sub["sim_ok"] == 1, sub["sim_detail"]
        assert sub["sim_method"] == ("simulateTransaction" if o["count"] == 1 else "simulateBundle")
        assert len(legs(app, sub["id"])) == o["count"]
    assert ch.accounts[harness.kp.pubkey()].lamports == bal0  # nothing executed
    assert not [c for c in ch.jito_calls if c["method"] in ("sendTransaction", "sendBundle")]


# ------------------------------------------------------------------------------------------------
# SELL + PnL
# ------------------------------------------------------------------------------------------------
async def test_manual_sell_partial_and_all_pnl(harness):
    app = await harness.start()
    ch = harness.chain
    order, _ = await app.orders.create_and_wait("NOW", str(ch.mint), params(3, sol="0.2"), None)
    await final_order(app, order["id"])
    pos = app.positions.open_position(str(ch.mint))
    cost_total = pos["cost_lamports"]
    held0 = int(pos["token_raw"])
    ch.move_price_to(1.3)  # price up 30%
    for pct in ("25", "50"):
        res = await app.engine.execute_sell(str(ch.mint), Decimal(pct), "MANUAL", None)
        assert res.status == "SENT"
        await wait_for(lambda: app.db.get("sells", app.db.one("SELECT max(id) m FROM sells")["m"])["status"]
                       in ("FILLED", "FAILED_SAFE"))
    pos = app.positions.get(pos["id"])
    assert pos["status"] == "OPEN"
    assert int(pos["token_raw"]) == ch.token_balance(harness.kp.pubkey())
    assert int(pos["token_raw"]) == held0 - held0 // 4 - (held0 - held0 // 4) // 2
    res = await app.engine.execute_sell(str(ch.mint), Decimal(100), "MANUAL", None)
    await wait_for(lambda: app.positions.get(pos["id"])["status"] == "CLOSED")
    pos = app.positions.get(pos["id"])
    assert ch.token_balance(harness.kp.pubkey()) == 0 and pos["token_raw"] == "0" and pos["cost_lamports"] == 0
    sells = app.db.query("SELECT * FROM sells ORDER BY id")
    assert sum(s["cost_alloc_lamports"] for s in sells) == cost_total
    assert pos["realized_pnl_lamports"] == pos["proceeds_total_lamports"] - cost_total
    assert pos["realized_pnl_lamports"] > 0


# ------------------------------------------------------------------------------------------------
# Auto Sell TP/SL
# ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind,move,reason", [("TP", 1.6, "TP"), ("SL", 0.75, "SL")])
async def test_auto_tp_sl(harness, kind, move, reason):
    app = await harness.start()
    ch = harness.chain
    order, _ = await app.orders.create_and_wait("NOW", str(ch.mint), params(1, sol="0.2", tp="20", sl="15"), None)
    o = await final_order(app, order["id"])
    pos = app.positions.open_position(str(ch.mint))
    assert pos["tpsl_enabled"] == 1
    ch.move_price_to(move)
    p = await wait_for(lambda: (lambda p: p if p["status"] == "CLOSED" else None)(app.positions.get(pos["id"])))
    sell = app.db.one("SELECT * FROM sells WHERE position_id=?", (pos["id"],))
    assert sell["reason"] == reason and sell["status"] == "FILLED"
    assert (p["realized_pnl_lamports"] > 0) == (reason == "TP")
    pnl_pct = Decimal(p["realized_pnl_lamports"]) / Decimal(p["cost_total_lamports"]) * 100
    assert pnl_pct >= 20 if reason == "TP" else pnl_pct <= -15


async def test_tpsl_not_active_before_buy_verified(harness):
    app = await harness.start(verify_deadline_s=2.0)
    ch = harness.chain
    ch.jito_mode = "drop"  # ACK but never lands
    order, res = await app.orders.create_and_wait("NOW", str(ch.mint), params(1, tp="10", sl="10"), None)
    assert res.status == "SENT"
    ch.move_price_to(0.5)
    await asyncio.sleep(0.3)
    assert app.positions.open_position(str(ch.mint)) is None
    assert not app.db.query("SELECT * FROM sells")


# ------------------------------------------------------------------------------------------------
# UNKNOWN/HOLD, reconciliation, no duplicate buys
# ------------------------------------------------------------------------------------------------
async def test_unknown_hold_blocks_duplicates_then_reconciles(harness):
    app = await harness.start(verify_deadline_s=0.5)
    ch = harness.chain
    notes = []
    app.engine.notify = lambda chat, text: notes.append(text)
    ch.jito_mode = "drop"
    ch.rpc_fail["vb.test"] = "error"  # second verification RPC down -> cannot prove anything
    order, res = await app.orders.create_and_wait("NOW", str(ch.mint), params(1), None)
    o = await final_order(app, order["id"])
    assert o["status"] == "HOLD"
    sub = app.db.get("submissions", o["submission_id"])
    assert sub["status"] == "UNKNOWN_HOLD"
    # new BUY on the same mint is blocked while HOLD
    o2, res2 = await app.orders.create_and_wait("NOW", str(ch.mint), params(1), None)
    assert res2.status == "PRESEND_FAILED" and "HOLD" in res2.message
    ch.advance(300)
    await asyncio.sleep(1.5)  # several reconcile passes
    assert app.db.get("submissions", sub["id"])["status"] == "UNKNOWN_HOLD"  # still cannot conclude
    assert sum(n.startswith("⚠️ Lệnh") and "UNKNOWN/HOLD" in n for n in notes) == 1  # once, not every pass
    ch.rpc_fail.pop("vb.test")
    o = await wait_for(lambda: (lambda o: o if o["status"] == "FAILED_SAFE" else None)(
        app.db.get("orders", order["id"])), timeout=5)
    assert app.db.get("submissions", sub["id"])["status"] == "FAILED_SAFE"
    assert ch.token_balance(harness.kp.pubkey()) == 0


async def test_ack_is_not_success_and_late_landing(harness):
    """Jito ACKs but the bundle lands only later: status goes ACK/PENDING -> SUCCESS."""
    app = await harness.start()
    ch = harness.chain
    ch.jito_mode = "drop"
    order, res = await app.orders.create_and_wait("NOW", str(ch.mint), params(3), None)
    assert res.status == "SENT"
    await asyncio.sleep(0.1)
    sub = app.db.get("submissions", app.db.get("orders", order["id"])["submission_id"])
    assert sub["status"] in ("ACK", "PENDING")
    assert app.db.get("orders", order["id"])["status"] == "SENT"
    # the very same signed bundle lands now
    raws = [__import__("base64").b64decode(t["raw_b64"]) for t in legs(app, sub["id"])]
    state = ch._copy()
    results = []
    for r in raws:
        rr = ch.execute(r, commit=False, state=state)
        assert rr["err"] is None
        state = rr["state"]
        results.append(rr)
    ch.accounts = state
    for rr in results:
        ch.land(rr)
    o = await final_order(app, order["id"])
    assert o["status"] == "FILLED"


async def test_jito_429_resends_identical_bytes_once_landed(harness):
    app = await harness.start()
    ch = harness.chain
    ch.jito_429_budget = 1
    order, res = await app.orders.create_and_wait("NOW", str(ch.mint), params(1), None)
    o = await final_order(app, order["id"])
    assert o["status"] == "FILLED"
    sends = [c for c in ch.jito_calls if c["method"] == "sendTransaction"]
    assert len(sends) == 2  # 429 then identical resend
    assert len(ch.txs) == 1  # landed exactly once


async def test_jito_unreachable_is_not_assumed_failed_until_expiry(harness):
    app = await harness.start(verify_deadline_s=3.0)
    ch = harness.chain
    ch.jito_mode = "http500"
    order, res = await app.orders.create_and_wait("NOW", str(ch.mint), params(1), None)
    assert res.status == "SENT" and "no ACK" in res.message
    await asyncio.sleep(0.2)
    assert app.db.get("orders", order["id"])["status"] == "SENT"  # unknown until expiry
    ch.advance(300)
    o = await final_order(app, order["id"])
    assert o["status"] == "FAILED_SAFE"


# ------------------------------------------------------------------------------------------------
# Simulation requirements for bundles
# ------------------------------------------------------------------------------------------------
async def test_bundle_refused_without_sequential_simulation(harness):
    app = await harness.start(bundle_sim_rpc_url=None)
    ch = harness.chain
    o, res = await app.orders.create_and_wait("NOW", str(ch.mint), params(3), None)
    assert res.status == "PRESEND_FAILED" and "Bundle" in res.message
    assert app.db.get("orders", o["id"])["status"] == "FAILED_SAFE"
    o1, res1 = await app.orders.create_and_wait("NOW", str(ch.mint), params(1), None)
    assert res1.status == "SENT"  # ×1 uses simulateTransaction


async def test_bundle_refused_when_rpc_lacks_simulate_bundle(harness):
    app = await harness.start()
    ch = harness.chain
    ch.supports_simulate_bundle = False
    o, res = await app.orders.create_and_wait("NOW", str(ch.mint), params(5), None)
    assert res.status == "PRESEND_FAILED" and "simulateBundle" in res.message
    assert not [c for c in ch.jito_calls if c["method"] == "sendBundle"]


async def test_simulation_failure_blocks_send(harness):
    """Risk checks bypassed on purpose: the simulation alone must stop an unexecutable transaction."""
    app = await harness.start()
    ch = harness.chain
    app.risk.check_buy = lambda **kw: None  # disable the balance pre-check
    ch.accounts[harness.kp.pubkey()].lamports = 50_000_000  # 0.05 SOL < 0.1 SOL buy
    o, res = await app.orders.create_and_wait("NOW", str(ch.mint), params(1, sol="0.1"), None)
    assert res.status == "PRESEND_FAILED" and "Mô phỏng thất bại" in res.message, res
    o3, res3 = await app.orders.create_and_wait("NOW", str(ch.mint), params(3, sol="0.1"), None)
    assert res3.status == "PRESEND_FAILED" and "simulateBundle" in res3.message, res3
    assert not [c for c in ch.jito_calls if c["method"] in ("sendTransaction", "sendBundle")]


async def test_identical_concurrent_orders_get_distinct_signatures(harness):
    app = await harness.start()
    ch = harness.chain
    lim = str(ch.spot_price() * Decimal("0.9"))
    a = await app.orders.create("LIMIT", str(ch.mint), params(1, limit=lim), None)
    b = await app.orders.create("LIMIT", str(ch.mint), params(1, limit=lim), None)
    ch.move_price_to(0.8)
    oa, ob = await final_order(app, a["id"]), await final_order(app, b["id"])
    assert oa["status"] == ob["status"] == "FILLED"
    sa = legs(app, oa["submission_id"])[0]["signature"]
    sb = legs(app, ob["submission_id"])[0]["signature"]
    assert sa != sb


# ------------------------------------------------------------------------------------------------
# Restart / recovery
# ------------------------------------------------------------------------------------------------
async def test_restart_restores_armed_orders_and_pending_verification(harness):
    app = await harness.start(verify_deadline_s=3.0)
    ch = harness.chain
    dip = await app.orders.create("DIP", str(ch.mint), params(3, dip="10", tp="30"), None)
    lim = await app.orders.create("LIMIT", str(ch.mint), params(1, limit=str(ch.spot_price() * Decimal("0.8"))),
                                  None)
    ch.jito_mode = "drop"
    now_o, _ = await app.orders.create_and_wait("NOW", str(ch.mint), params(1), None)
    await harness.stop(app)  # crash/restart while the NOW submission is still unverified
    ch.jito_mode = "land"
    app2 = await harness.start(verify_deadline_s=3.0)
    rep = app2.recovery_report
    assert rep["armed_restored"] == 2 and len(rep["resume_verify"]) == 1
    d2 = app2.db.get("orders", dip["id"])
    assert d2["status"] == "ARMED" and d2["trigger_price"] == dip["trigger_price"]
    assert d2["reference_price"] == dip["reference_price"]
    ch.advance(300)  # the dropped NOW buy expires -> FAILED_SAFE, never re-sent
    o = await final_order(app2, now_o["id"])
    assert o["status"] == "FAILED_SAFE"
    ch.new_blockhash()
    await app2.blockhash.refresh()
    ch.move_price_to(0.75)
    d = await final_order(app2, dip["id"])
    l = await final_order(app2, lim["id"])
    assert d["status"] == "FILLED" and l["status"] == "FILLED"
    assert app2.db.one("SELECT COUNT(*) c FROM submissions WHERE order_id=?", (now_o["id"],))["c"] == 1


async def test_restart_never_resends_prepared_or_triggered(harness):
    app = await harness.start()
    ch = harness.chain
    db = app.db
    import time
    oid = db.insert("orders", {"kind": "DIP", "mint": str(ch.mint), "route": "pumpswap", "count": 1,
                               "sol_per_buy_lamports": 10**8, "slippage_bps": 100, "priority_micro_lamports": 0,
                               "cu_limit": 200000, "tip_lamports": 10000, "trigger_price": "1", "exec_mode": "LIVE",
                               "status": "TRIGGERED", "created_at": time.time()})
    sid = db.insert("submissions", {"purpose": "BUY", "order_id": oid, "mint": str(ch.mint), "route": "pumpswap",
                                    "count": 1, "exec_mode": "LIVE", "status": "PREPARED", "created_at": time.time()})
    db.update("orders", oid, {"submission_id": sid})
    await harness.stop(app)
    app2 = await harness.start()
    assert app2.recovery_report["never_sent"] == 1
    assert app2.db.get("orders", oid)["status"] == "FAILED_SAFE"
    assert app2.db.get("submissions", sid)["status"] == "FAILED_SAFE"
    await asyncio.sleep(0.2)
    assert not [c for c in ch.jito_calls if c["method"] in ("sendTransaction", "sendBundle")]


# ------------------------------------------------------------------------------------------------
# Data freshness & connectivity
# ------------------------------------------------------------------------------------------------
async def test_stale_snapshot_never_triggers(harness):
    app = await harness.start()
    ch = harness.chain
    order = await app.orders.create("LIMIT", str(ch.mint), params(1, limit=str(ch.spot_price() * 2)), None)
    # trigger condition already true; make every snapshot stale by stopping the feed
    m = app.market.markets[ch.mint]
    await asyncio.sleep(0.05)
    o = app.db.get("orders", order["id"])
    if o["status"] == "ARMED":  # (it may already have fired on a fresh snapshot — that's valid)
        pass
    snap = m.snapshot
    import copy
    stale = copy.copy(snap)
    stale.received_ms -= 10_000
    stale.confirmed_ms -= 10_000
    app.triggers.busy.clear()
    before = app.db.one("SELECT COUNT(*) c FROM submissions")["c"]
    app.triggers.on_snapshot(stale)
    await asyncio.sleep(0.05)
    assert app.db.one("SELECT COUNT(*) c FROM submissions")["c"] == before


async def test_rpc_429_failover(harness):
    app = await harness.start()
    ch = harness.chain
    ch.rpc_fail["rpc1.test"] = "429"
    _, accs = await app.rpc.get_multiple_accounts([ch.mint])
    assert accs[0] is not None
    ch.rpc_fail["rpc1.test"] = "timeout"
    _, accs = await app.rpc.get_multiple_accounts([ch.mint])
    assert accs[0] is not None


async def test_live_order_does_not_fire_while_bot_in_no_send(harness):
    app = await harness.start()
    ch = harness.chain
    order = await app.orders.create("DIP", str(ch.mint), params(1, dip="5"), None)
    assert order["exec_mode"] == "LIVE"
    app.settings.set_mode("NO_SEND", confirmed_by_user=True)
    ch.move_price_to(0.8)
    await asyncio.sleep(0.2)
    assert app.db.get("orders", order["id"])["status"] == "ARMED"
    app.settings.set_mode("LIVE", confirmed_by_user=True)
    ch.external_trade("sell", base=10**9)
    o = await final_order(app, order["id"])
    assert o["status"] == "FILLED"


async def test_cancel_armed_order(harness):
    app = await harness.start()
    ch = harness.chain
    order = await app.orders.create("LIMIT", str(ch.mint), params(3, limit=str(ch.spot_price() * Decimal("0.5"))),
                                    None)
    assert app.orders.cancel(order["id"])
    assert app.db.get("orders", order["id"])["status"] == "CANCELLED"
    ch.move_price_to(0.4)
    await asyncio.sleep(0.2)
    assert app.db.get("orders", order["id"])["status"] == "CANCELLED"
    assert not app.orders.cancel(order["id"])  # cannot cancel twice


async def test_stale_rpc_node_is_skipped(harness):
    from solbot.rpc import RpcStale

    app = await harness.start()
    ch = harness.chain
    app.rpc.observe_slot(ch.slot)
    ch.rpc_slot_lag["rpc1.test"] = 500  # rpc1 lags 500 slots behind the best known slot
    seen = []
    orig = app.rpc._check_ctx

    def spy(ep, res):
        try:
            orig(ep, res)
            seen.append((ep.name, "ok"))
        except RpcStale:
            seen.append((ep.name, "stale"))
            raise

    app.rpc._check_ctx = spy
    slot, accs = await app.rpc.get_multiple_accounts([ch.mint])
    assert accs[0] is not None and slot == ch.slot  # answer came from the healthy node
    assert ("rpc1.test", "stale") in seen and ("rpc2.test", "ok") in seen


async def test_snapshot_older_than_600ms_before_send_is_reprepared(harness):
    app = await harness.start()
    ch = harness.chain
    real = app.simulator.simulate
    calls = {"n": 0}

    async def slow_first(txs, expects, min_slot=None):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(0.7)  # the plan's snapshot is now > 600 ms old
        return await real(txs, expects, min_slot)

    app.simulator.simulate = slow_first
    o, res = await app.orders.create_and_wait("NOW", str(ch.mint), params(3), None)
    assert res.status == "SENT", res
    assert calls["n"] == 2  # first preparation discarded, prepared again with fresh data
    sends = [c for c in ch.jito_calls if c["method"] == "sendBundle"]
    assert len(sends) == 1
    sub = app.db.get("submissions", res.submission_id)
    assert sub["snapshot_age_ms"] <= 600
    assert (await final_order(app, o["id"]))["status"] == "FILLED"


async def test_dip_with_100pct_slippage_never_above_ceiling(harness):
    app = await harness.start(verify_deadline_s=3.0)
    ch = harness.chain
    order = await app.orders.create("DIP", str(ch.mint), params(5, slip="100", dip="10"), None)
    trig = Decimal(order["trigger_price"])
    ch.before_land_hook = lambda: ch.move_price_to(1.15)  # adversarial pump just before landing
    ch.move_price_to(0.85)
    await wait_for(lambda: app.db.get("orders", order["id"])["status"] == "SENT", timeout=5)
    sub_id = app.db.get("orders", order["id"])["submission_id"]
    for l in legs(app, sub_id):
        # even with 100% slippage the on-chain maxIn is bounded by the ceiling
        assert l["max_in_lamports"] <= int(Decimal(l["planned_out_raw"]) * trig * 10**9 / 10**6)
    assert ch.token_balance(harness.kp.pubkey()) == 0  # guard rejected the bundle on-chain
    ch.advance(300)
    assert (await final_order(app, order["id"]))["status"] == "FAILED_SAFE"


async def test_rpc_outage_at_trigger_rearms_dip_without_sending(harness):
    app = await harness.start()
    ch = harness.chain
    order = await app.orders.create("DIP", str(ch.mint), params(3, dip="10"), None)
    real = app.engine._user_accounts
    calls = {"n": 0}

    async def flaky(mint, tp, max_age_ms=0):
        if max_age_ms > 0:  # the trigger hot path
            calls["n"] += 1
            if calls["n"] <= 2:
                from solbot.rpc import RpcTransportError
                raise RpcTransportError("rpc down")
        return await real(mint, tp, max_age_ms)

    app.engine._user_accounts = flaky
    ch.move_price_to(0.85)
    o = await final_order(app, order["id"])
    assert o["status"] == "FILLED" and o["infra_rearms"] == 2
    assert app.db.one("SELECT COUNT(*) c FROM submissions WHERE order_id=?", (order["id"],))["c"] == 1


async def test_simulation_rate_limit_fails_over_and_never_burns_dip(harness):
    """Primary simulate endpoint returns 429 -> the second one simulates. When every simulate endpoint
    is rate-limited at trigger time, nothing is sent and the DIP goes back to ARMED (not consumed)."""
    app = await harness.start(sim_rpc_url="https://rpc1.test,https://rpc2.test")
    ch = harness.chain
    ch.rpc_fail_method[("rpc1.test", "simulateTransaction")] = "429"
    o, res = await app.orders.create_and_wait("NOW", str(ch.mint), params(1), None)
    assert res.status == "SENT", res
    ch.rpc_fail_method[("rpc2.test", "simulateTransaction")] = "429"
    order = await app.orders.create("DIP", str(ch.mint), params(1, dip="10"), None)
    ch.move_price_to(0.85)
    await wait_for(lambda: app.db.get("orders", order["id"])["infra_rearms"] >= 1, timeout=5)
    assert app.db.get("orders", order["id"])["status"] in ("ARMED", "TRIGGERED")
    assert app.db.one("SELECT COUNT(*) c FROM submissions WHERE order_id=?", (order["id"],))["c"] == 0
    ch.rpc_fail_method.clear()
    o = await final_order(app, order["id"])
    assert o["status"] == "FILLED"


async def test_simulation_never_runs_on_a_bank_older_than_the_snapshot(harness):
    """Sim node lagging behind the snapshot slot (-32016) -> another node simulates."""
    app = await harness.start(sim_rpc_url="https://rpc1.test,https://rpc2.test")
    ch = harness.chain
    ch.rpc_slot_lag["rpc1.test"] = 5  # first sim node is 5 slots behind
    o, res = await app.orders.create_and_wait("NOW", str(ch.mint), params(1), None)
    assert res.status == "SENT", res
    assert (await final_order(app, o["id"]))["status"] == "FILLED"
