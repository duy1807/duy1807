"""Jupiter route end-to-end (fake Jupiter API + emulated route program on the fake chain)."""
from decimal import Decimal

import pytest

from solbot.orders import OrderParams

from .conftest import wait_for


def P(count, **kw):
    base = dict(sol_per_buy=Decimal("0.1"), count=count, slippage_pct=Decimal(5), tp_pct=Decimal(0),
                sl_pct=Decimal(0))
    base.update(kw)
    return OrderParams(**base)


async def final(app, oid):
    return await wait_for(lambda: (lambda o: o if o["status"] in ("FILLED", "FAILED_SAFE", "HOLD", "DRY_RUN")
                                   else None)(app.db.get("orders", oid)))


@pytest.mark.parametrize("count", [1, 3, 5])
async def test_jupiter_buy_now(harness, count):
    app = await harness.start(jupiter_price_poll_ms=30)
    app.settings.set("route", "jupiter")
    ch = harness.chain
    o, res = await app.orders.create_and_wait("NOW", str(ch.mint), P(count), None)
    assert res.status == "SENT", res
    assert o["route"] == "jupiter"
    o = await final(app, o["id"])
    assert o["status"] == "FILLED"
    legs = app.db.query("SELECT * FROM sub_txs WHERE submission_id=? ORDER BY idx", (o["submission_id"],))
    for l in legs:
        assert int(l["actual_token_delta"]) >= int(l["min_out_raw"])  # on-chain minimum honoured
    assert int(app.positions.open_position(str(ch.mint))["token_raw"]) == ch.token_balance(harness.kp.pubkey())


async def test_jupiter_limit_x3_and_sell(harness):
    app = await harness.start(jupiter_price_poll_ms=30)
    app.settings.set("route", "jupiter")
    ch = harness.chain
    lim = ch.spot_price() * Decimal("0.9")
    o = await app.orders.create("LIMIT", str(ch.mint), P(3, limit_price=lim), None)
    ch.move_price_to(0.85)
    o = await final(app, o["id"])
    assert o["status"] == "FILLED"
    for l in app.db.query("SELECT * FROM sub_txs WHERE submission_id=?", (o["submission_id"],)):
        assert Decimal(l["exec_price"]) <= lim
    res = await app.engine.execute_sell(str(ch.mint), Decimal(100), "MANUAL", None)
    assert res.status == "SENT"
    await wait_for(lambda: app.positions.open_position(str(ch.mint)) is None)
    assert ch.token_balance(harness.kp.pubkey()) == 0


async def test_jupiter_weaker_encoded_min_is_refused_for_limit(harness):
    app = await harness.start(jupiter_price_poll_ms=30)
    app.settings.set("route", "jupiter")
    ch = harness.chain
    ch.jup_malicious = True
    o = await app.orders.create("LIMIT", str(ch.mint), P(1, limit_price=ch.spot_price() * Decimal("0.95")), None)
    ch.move_price_to(0.85)
    o = await final(app, o["id"])
    assert o["status"] == "FAILED_SAFE" and "minOut" in o["status_reason"]
    assert not [c for c in ch.jito_calls if c["method"] in ("sendTransaction", "sendBundle")]
