from decimal import Decimal


from solbot.orders import OrderParams
from .conftest import wait_for


async def test_buy_now_x1_live(harness):
    app = await harness.start()
    ch = harness.chain
    p = OrderParams(sol_per_buy=Decimal("0.1"), count=1, slippage_pct=Decimal("5"), tp_pct=Decimal(0), sl_pct=Decimal(0))
    order, res = await app.orders.create_and_wait("NOW", str(ch.mint), p, chat_id=None)
    print(res, order["status"], order["status_reason"])
    assert res.status == "SENT", res
    o = await wait_for(lambda: (lambda o: o if o["status"] in ("FILLED", "FAILED_SAFE", "HOLD") else None)(
        app.db.get("orders", order["id"])))
    print(o)
    assert o["status"] == "FILLED"


async def test_buy_now_x5_details(harness):
    app = await harness.start()
    ch = harness.chain
    p = OrderParams(sol_per_buy=Decimal("0.3"), count=5, slippage_pct=Decimal("5"), tp_pct=Decimal(20), sl_pct=Decimal(10))
    order, res = await app.orders.create_and_wait("NOW", str(ch.mint), p, chat_id=None)
    assert res.status == "SENT", res
    o = await wait_for(lambda: (lambda o: o if o["status"] in ("FILLED", "FAILED_SAFE", "HOLD") else None)(
        app.db.get("orders", order["id"])))
    assert o["status"] == "FILLED", o
    txs = app.db.query("SELECT idx,status,planned_out_raw,max_in_lamports,actual_token_delta,actual_sol_delta,fee_lamports,tip_paid_lamports,rent_lamports,swap_lamports,exec_price,planned_price,length(raw_b64)*3/4 as sz FROM sub_txs ORDER BY idx")
    for t in txs: print(t)
    sub = app.db.get("submissions", o["submission_id"]); print({k: sub[k] for k in ("status","sim_method","sim_detail","bundle_id","verify_level")})
    pos = app.positions.open_position(str(ch.mint)); print(pos)
    print("chain token balance", ch.token_balance(harness.kp.pubkey()))
