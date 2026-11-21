"""Yellowstone gRPC subscriber.

One bidirectional Subscribe stream carries:
  * account filter ``pools``: Pool + base vault + quote vault of every watched PumpSwap market;
  * slot filter ``hb``: slot updates used as stream heartbeat (snapshot freshness).
Changing the watched set re-sends a full SubscribeRequest on the same stream (Yellowstone replaces
filters). On disconnect all markets are marked unsynced (their snapshots stop being refreshed, so
trading code sees them as stale) and the stream reconnects with back-off, then resyncs over RPC.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

import grpc
from solders.pubkey import Pubkey

from .grpc_gen import geyser_pb2 as pb
from .grpc_gen import geyser_pb2_grpc as pb_grpc
from .market import MarketEngine, TokenMarket

log = logging.getLogger("solbot.geyser")

_COMMITMENT = {"processed": pb.CommitmentLevel.PROCESSED, "confirmed": pb.CommitmentLevel.CONFIRMED,
               "finalized": pb.CommitmentLevel.FINALIZED}


class GeyserSource:
    def __init__(self, engine: MarketEngine, endpoint: str, token: str | None, tls: bool = True,
                 commitment: str = "processed", root_certs: bytes | None = None):
        self.engine = engine
        self.endpoint = endpoint
        self.token = token
        self.tls = tls
        self.commitment = _COMMITMENT.get(commitment, pb.CommitmentLevel.PROCESSED)
        self.root_certs = root_certs
        self.last_error = ""
        self.routes: dict[bytes, tuple[TokenMarket, str]] = {}
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self.connected = False
        self.last_msg_ms = 0.0
        self.reconnects = 0
        self._ping_id = 0

    # -- subscription set -------------------------------------------------------------------
    def add_market(self, m: TokenMarket) -> None:
        assert m.pool
        self.routes[bytes(m.pool.address)] = (m, "pool")
        self.routes[bytes(m.pool.pool_base_token_account)] = (m, "base")
        self.routes[bytes(m.pool.pool_quote_token_account)] = (m, "quote")
        self._resubscribe()

    def remove_market(self, m: TokenMarket) -> None:
        if m.pool:
            for k in (m.pool.address, m.pool.pool_base_token_account, m.pool.pool_quote_token_account):
                self.routes.pop(bytes(k), None)
        self._resubscribe()

    def _request(self) -> pb.SubscribeRequest:
        req = pb.SubscribeRequest()
        accts = [str(Pubkey.from_bytes(k)) for k in self.routes]
        if accts:
            req.accounts["pools"].account.extend(accts)
        req.slots["hb"].CopyFrom(pb.SubscribeRequestFilterSlots(filter_by_commitment=True))
        req.commitment = self.commitment
        return req

    def _resubscribe(self) -> None:
        if self._queue is not None and self.connected:
            self._queue.put_nowait(self._request())

    # -- lifecycle --------------------------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="geyser")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def _channel(self) -> grpc.aio.Channel:
        opts = [("grpc.max_receive_message_length", 64 * 1024 * 1024),
                ("grpc.keepalive_time_ms", 10_000), ("grpc.keepalive_timeout_ms", 5_000),
                ("grpc.http2.max_pings_without_data", 0), ("grpc.keepalive_permit_without_calls", 1)]
        if self.tls:
            creds = grpc.ssl_channel_credentials(root_certificates=self.root_certs)
            return grpc.aio.secure_channel(self.endpoint, creds, options=opts)
        return grpc.aio.insecure_channel(self.endpoint, options=opts)

    async def _run(self) -> None:
        backoff = 0.5
        while True:
            try:
                await self._session()
                backoff = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as e:
                details = e.details() if isinstance(e, grpc.aio.AioRpcError) else None
                self.last_error = str(details or e)[:200]
                log.warning("geyser stream error (HTTP polling fallback active): %s", self.last_error)
            self.connected = False
            self.engine.on_stream_down()
            self.reconnects += 1
            await asyncio.sleep(backoff + random.random() * 0.2)
            backoff = min(backoff * 2, 10)

    async def _session(self) -> None:
        self._queue = asyncio.Queue()
        md = (("x-token", self.token),) if self.token else None
        async with self._channel() as ch:
            stub = pb_grpc.GeyserStub(ch)
            queue = self._queue

            async def requests():
                yield self._request()
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=10)
                    except asyncio.TimeoutError:
                        self._ping_id += 1
                        item = pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=self._ping_id))
                    yield item

            call = stub.Subscribe(requests(), metadata=md)
            self.connected = True
            log.info("geyser connected to %s", self.endpoint.split(":")[0])
            # resync base state of every watched market (updates may have been missed)
            for m in {str(v[0].mint): v[0] for v in self.routes.values()}.values():
                try:
                    await self.engine.fetch_now(m, resync=True)
                except Exception as e:
                    log.warning("resync %s failed: %s", m.mint, e)
            async for upd in call:
                self.last_msg_ms = time.monotonic() * 1000
                which = upd.WhichOneof("update_oneof")
                if which == "account":
                    a = upd.account
                    route = self.routes.get(bytes(a.account.pubkey))
                    if route is None:
                        continue
                    m, role = route
                    sig = bytes(a.account.txn_signature) if a.account.HasField("txn_signature") else None
                    self.engine.on_account_update(m, role, int(a.slot), bytes(a.account.data), sig)
                elif which == "slot":
                    self.engine.on_heartbeat(int(upd.slot.slot))
                elif which == "ping":
                    pass
