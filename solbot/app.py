"""Application wiring and lifecycle."""
from __future__ import annotations

import asyncio
import logging
import signal

import httpx

from .autosell import AutoSell
from .config import Config
from .db import Database
from .dex.jupiter import JupiterClient
from .engine import TradeEngine
from .jito import JitoClient
from .market import BlockhashCache, MarketEngine
from .orders import OrderService
from .positions import PositionManager
from .recovery import Recovery
from .risk import RiskManager
from .rpc import HttpJsonRpc, RpcPool
from .settings import Settings
from .simulation import Simulator
from .triggers import TriggerManager
from .verify import Verifier
from .wallet import Wallet, load_wallet

log = logging.getLogger("solbot.app")


class App:
    def __init__(self, config: Config, wallet: Wallet | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 enable_telegram: bool = True, enable_background: bool = True):
        self.config = config
        self.transport = transport
        self.enable_background = enable_background
        self.db = Database(config.db_path)
        self.settings = Settings(self.db, config)
        self.wallet = wallet or load_wallet(config.wallet_keypair_path, config.wallet_encrypted_path,
                                            config.wallet_passphrase_env)
        t = config.rpc_timeout_ms

        def client(headers: dict | None = None, timeout_ms: int = t) -> httpx.AsyncClient:
            # keep idle connections for 90 s (httpx default is 5 s): a cold TLS handshake costs 0.3–1.4 s
            return httpx.AsyncClient(transport=transport, timeout=timeout_ms / 1000,
                                     headers={"content-type": "application/json", **(headers or {})},
                                     limits=httpx.Limits(max_keepalive_connections=20, max_connections=50,
                                                         keepalive_expiry=90))

        def ep(url: str) -> HttpJsonRpc:
            return HttpJsonRpc(url, t, client=client())

        self.rpc = RpcPool([ep(u) for u in config.rpc_urls], max_slot_lag=config.rpc_max_slot_lag)
        sim_urls = [u.strip() for u in (config.sim_rpc_url or "").split(",") if u.strip()] or list(config.rpc_urls)
        self.sim_rpcs = [ep(u) for u in sim_urls]
        self.sim_rpc = self.sim_rpcs[0]
        self.bundle_sim_rpc = ep(config.bundle_sim_rpc_url) if config.bundle_sim_rpc_url else None
        va = config.verify_rpc_a_url or config.primary_rpc
        vb = config.verify_rpc_b_url or config.rpc_urls[-1]
        self.verify_independent = bool(config.verify_rpc_a_url and config.verify_rpc_b_url and va != vb)
        self.rpc_a = RpcPool([ep(va)], max_attempts=2)
        self.rpc_b = RpcPool([ep(vb)], max_attempts=2)
        self.jupiter = JupiterClient(config.jupiter_base_url, config.jupiter_api_key,
                                     client=httpx.AsyncClient(
                                         transport=transport, timeout=2.5,
                                         headers={"accept": "application/json",
                                                  **({"x-api-key": config.jupiter_api_key}
                                                     if config.jupiter_api_key else {})}),
                                     max_accounts=config.jupiter_max_accounts)
        jito_headers = {"x-jito-auth": config.jito_uuid} if config.jito_uuid else {}
        self.jito = JitoClient(config.jito_url, config.jito_extra_urls, config.jito_uuid, config.jito_timeout_ms,
                               config.jito_identical_resend, client=client(jito_headers, config.jito_timeout_ms))
        self.market = MarketEngine(config, self.rpc, self.db, self.jupiter)
        self.geyser = None
        if config.grpc_endpoint:
            from .geyser import GeyserSource

            roots = config.grpc_root_certs.read_bytes() if config.grpc_root_certs else None
            self.geyser = GeyserSource(self.market, config.grpc_endpoint, config.grpc_token, config.grpc_tls,
                                       config.grpc_commitment, roots)
            self.market.geyser = self.geyser
        self.blockhash = BlockhashCache(self.rpc, config.blockhash_max_age_ms)
        self.simulator = Simulator(self.sim_rpcs, self.bundle_sim_rpc)
        self.verifier = Verifier(self.db, self.rpc_a, self.rpc_b, config.verify_poll_ms,
                                 config.verify_expiry_margin_blocks, self.verify_independent)
        self.risk = RiskManager(self.db, self.settings, config)
        self.positions = PositionManager(self.db, str(self.wallet.pubkey))
        self.engine = TradeEngine(config=config, settings=self.settings, db=self.db, wallet=self.wallet,
                                  rpc=self.rpc, market=self.market, blockhash=self.blockhash,
                                  simulator=self.simulator, jito=self.jito, verifier=self.verifier,
                                  risk=self.risk, positions=self.positions, jupiter=self.jupiter)
        self.triggers = TriggerManager(self.db, self.market, self.engine, self.settings, config.snapshot_max_age_ms)
        self.autosell = AutoSell(self.db, self.market, self.engine, self.positions, self.settings,
                                 config.snapshot_max_age_ms)
        self.orders = OrderService(self.db, self.settings, self.market, self.engine, self.triggers)
        self.recovery = Recovery(self.db, self.verifier, config.reconcile_interval_ms, config.verify_deadline_s)
        self.telegram = None
        if enable_telegram and config.telegram_token:
            from .telegram_bot import TelegramBot

            self.telegram = TelegramBot(self, transport=transport)
            self.engine.notify = self.telegram.notify
        self.recovery_report: dict = {}

    def all_rpc_endpoints(self) -> list[HttpJsonRpc]:
        eps = list(self.rpc.endpoints) + list(self.sim_rpcs) + list(self.rpc_a.endpoints) + list(self.rpc_b.endpoints)
        if self.bundle_sim_rpc is not None:
            eps.append(self.bundle_sim_rpc)
        seen, out = set(), []
        for e in eps:
            if id(e) not in seen:
                seen.add(id(e))
                out.append(e)
        return out

    async def warm_connections(self) -> None:
        """Open/refresh the TLS connection of every endpoint used on the hot path (RPC, simulate,
        simulateBundle, verify A/B, Jito) so a trigger never pays a handshake."""
        async def one(ep):
            try:
                await ep.call("getSlot", [{"commitment": "processed"}], timeout=3)
            except Exception:
                pass

        await asyncio.gather(*(one(e) for e in self.all_rpc_endpoints()), self.jito.refresh_tip_accounts(),
                             return_exceptions=True)

    def _start_keepalive(self) -> None:
        async def loop():
            while True:
                await asyncio.sleep(self.config.keepalive_interval_s)
                await self.warm_connections()

        self._keepalive_task = asyncio.create_task(loop(), name="keepalive")

    async def start(self) -> None:
        log.info("starting solbot wallet=%s mode=%s config=%s", self.wallet.pubkey, self.settings.mode,
                 self.config.describe())
        if not self.verify_independent:
            log.warning("VERIFY_RPC_A_URL / VERIFY_RPC_B_URL are not two independent endpoints")
        self.recovery_report = self.recovery.recover_db()
        try:
            await self.market.refresh_configs()
        except Exception as e:
            log.warning("could not load PumpSwap configs yet: %s", e)
        try:
            await self.blockhash.refresh()
        except Exception as e:
            log.warning("blockhash not available yet: %s", e)
        await self.warm_connections()
        self.recovery.resume(self.recovery_report["resume_verify"])
        armed = await self.triggers.restore()
        opened = await self.autosell.restore()
        self.recovery_report.update({"armed_restored": armed, "positions_restored": opened})
        if self.enable_background:
            self.blockhash.start()
            self.market.start()
            if self.geyser:
                self.geyser.start()
            self.recovery.start()
            self.triggers.start()
            self._start_keepalive()
            self.autosell.start(max(self.config.jupiter_price_poll_ms, 1000))
        if self.telegram:
            await self.telegram.start()
        log.info("recovery: %s", self.recovery_report)

    async def stop(self) -> None:
        if self.telegram:
            await self.telegram.stop()
        if getattr(self, "_keepalive_task", None):
            self._keepalive_task.cancel()
        await self.recovery.stop()
        await self.triggers.stop()
        await self.autosell.stop()
        if self.geyser:
            await self.geyser.stop()
        await self.market.stop()
        await self.blockhash.stop()
        for tset in (self.engine._tasks,):
            for t in list(tset):
                t.cancel()
        await self.rpc.aclose()
        for extra in (*self.sim_rpcs, self.bundle_sim_rpc):
            if extra is not None:
                await extra.aclose()
        await self.rpc_a.aclose()
        await self.rpc_b.aclose()
        await self.jito.aclose()
        await self.jupiter.aclose()
        self.db.close()


async def run(config: Config) -> None:
    app = App(config)
    await app.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass
    await stop.wait()
    await app.stop()
