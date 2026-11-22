"""Yellowstone gRPC path against an in-process gRPC server implementing geyser.Geyser/Subscribe."""
import asyncio
from decimal import Decimal

import grpc
import pytest
from solders.pubkey import Pubkey

from solbot.grpc_gen import geyser_pb2 as pb
from solbot.grpc_gen import geyser_pb2_grpc as pb_grpc
from solbot.orders import OrderParams

from .conftest import wait_for


class FakeGeyser(pb_grpc.GeyserServicer):
    def __init__(self):
        self.out: asyncio.Queue = asyncio.Queue()
        self.requests = []
        self.sessions = 0
        self.metadata = []

    async def Subscribe(self, request_iterator, context):
        self.sessions += 1
        self.metadata.append(dict(context.invocation_metadata()))

        async def reader():
            async for r in request_iterator:
                self.requests.append(r)

        t = asyncio.create_task(reader())
        try:
            while True:
                u = await self.out.get()
                if u is None:
                    return
                yield u
        finally:
            t.cancel()

    def subscribed_accounts(self):
        for r in reversed(self.requests):
            if "pools" in r.accounts:
                return set(r.accounts["pools"].account)
        return set()


@pytest.fixture
async def geyser_server():
    fake = FakeGeyser()
    server = grpc.aio.server()
    pb_grpc.add_GeyserServicer_to_server(fake, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    fake.endpoint = f"127.0.0.1:{port}"
    yield fake
    await server.stop(0)


def acc_update(chain, key: Pubkey, slot: int, sig: bytes) -> pb.SubscribeUpdate:
    a = chain.accounts[key]
    info = pb.SubscribeUpdateAccountInfo(pubkey=bytes(key), lamports=a.lamports, owner=bytes(a.owner),
                                         data=a.data, write_version=1, txn_signature=sig)
    return pb.SubscribeUpdate(filters=["pools"], account=pb.SubscribeUpdateAccount(account=info, slot=slot))


def slot_update(slot: int) -> pb.SubscribeUpdate:
    return pb.SubscribeUpdate(filters=["hb"], slot=pb.SubscribeUpdateSlot(slot=slot, status=0))


async def push_trade(fake, chain, sig: bytes, roles=("pool", "base", "quote")):
    keys = {"pool": chain.pool_addr, "base": chain.base_vault, "quote": chain.quote_vault}
    for r in roles:
        await fake.out.put(acc_update(chain, keys[r], chain.slot, sig))


async def test_geyser_stream_snapshots_heartbeat_and_dip(harness, geyser_server):
    fake = geyser_server
    app = await harness.start(grpc_endpoint=fake.endpoint, grpc_tls=False, grpc_token="tok-123")
    ch = harness.chain
    order = await app.orders.create("DIP", str(ch.mint), OrderParams(
        sol_per_buy=Decimal("0.1"), count=3, slippage_pct=Decimal(5), tp_pct=Decimal(0), sl_pct=Decimal(0),
        dip_pct=Decimal(10)), None)
    await wait_for(lambda: {str(ch.pool_addr), str(ch.base_vault), str(ch.quote_vault)} <= fake.subscribed_accounts())
    assert fake.metadata[0].get("x-token") == "tok-123"
    m = app.market.markets[ch.mint]
    await fake.out.put(slot_update(ch.slot))
    await wait_for(lambda: m.synced and m.snapshot is not None)
    # heartbeat keeps the unchanged state fresh
    await asyncio.sleep(0.7)
    assert m.snapshot.age_ms() > 600
    await fake.out.put(slot_update(ch.slot + 1))
    await wait_for(lambda: m.snapshot.age_ms() < 100)
    # a trade arrives as three account updates with the same signature -> one consistent snapshot
    v0 = m.version
    ch.external_trade("sell", base=ch.pool_state().base_reserve // 200)
    await push_trade(fake, ch, b"S" * 64)
    await wait_for(lambda: m.version == v0 + 1)
    assert m.snapshot.source == "grpc" and m.snapshot.price_sol == ch.spot_price()
    # partial group (only base vault) commits after the grace window
    v1 = m.version
    ch.external_trade("sell", base=ch.pool_state().base_reserve // 200)
    await push_trade(fake, ch, b"T" * 64, roles=("base",))
    await wait_for(lambda: m.version == v1 + 1)
    # price drop through gRPC triggers the DIP bundle
    ch.move_price_to(0.85)
    await push_trade(fake, ch, b"U" * 64)
    o = await wait_for(lambda: (lambda o: o if o["status"] in ("FILLED", "FAILED_SAFE") else None)(
        app.db.get("orders", order["id"])))
    assert o["status"] == "FILLED"
    for l in app.db.query("SELECT * FROM sub_txs WHERE submission_id=?", (o["submission_id"],)):
        assert Decimal(l["exec_price"]) <= Decimal(o["trigger_price"])


async def test_geyser_disconnect_marks_stale_and_resyncs(harness, geyser_server):
    fake = geyser_server
    app = await harness.start(grpc_endpoint=fake.endpoint, grpc_tls=False)
    ch = harness.chain
    order = await app.orders.create("LIMIT", str(ch.mint), OrderParams(
        sol_per_buy=Decimal("0.1"), count=1, slippage_pct=Decimal(5), tp_pct=Decimal(0), sl_pct=Decimal(0),
        limit_price=ch.spot_price() * Decimal("0.9")), None)
    m = app.market.markets[ch.mint]
    await wait_for(lambda: fake.sessions == 1 and fake.subscribed_accounts())
    await fake.out.put(slot_update(ch.slot))
    await wait_for(lambda: m.synced)
    await fake.out.put(None)  # server closes the stream
    await wait_for(lambda: not m.synced)
    # while gRPC is down the HTTP polling fallback keeps snapshots fresh: the LIMIT still fires correctly
    ch.move_price_to(0.8)
    o = await wait_for(lambda: (lambda o: o if o["status"] == "FILLED" else None)(app.db.get("orders", order["id"])),
                       timeout=5)
    sub = app.db.get("submissions", o["submission_id"])
    assert sub["snapshot_age_ms"] <= 600
    # the stream reconnects, resubscribes and resyncs
    await wait_for(lambda: fake.sessions >= 2, timeout=5)
    await fake.out.put(slot_update(ch.slot))
    await wait_for(lambda: m.synced and app.market.grpc_healthy(), timeout=5)
    assert app.geyser.reconnects >= 1


class RejectingGeyser(pb_grpc.GeyserServicer):
    async def Subscribe(self, request_iterator, context):
        await context.abort(grpc.StatusCode.PERMISSION_DENIED, "requires a personal token")
        yield  # pragma: no cover


async def test_grpc_rejected_falls_back_to_http_polling(harness):
    server = grpc.aio.server()
    pb_grpc.add_GeyserServicer_to_server(RejectingGeyser(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        app = await harness.start(grpc_endpoint=f"127.0.0.1:{port}", grpc_tls=False)
        ch = harness.chain
        order = await app.orders.create("LIMIT", str(ch.mint), OrderParams(
            sol_per_buy=Decimal("0.1"), count=3, slippage_pct=Decimal(5), tp_pct=Decimal(0), sl_pct=Decimal(0),
            limit_price=ch.spot_price() * Decimal("0.9")), None)
        await asyncio.sleep(0.3)
        assert not app.market.grpc_healthy() and "personal token" in app.geyser.last_error
        ch.move_price_to(0.85)
        await wait_for(lambda: app.db.get("orders", order["id"])["status"] == "FILLED")
        assert app.market.markets[ch.mint].snapshot.source == "poll"
    finally:
        await server.stop(0)
