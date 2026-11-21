import asyncio
from pathlib import Path

import pytest
from solders.keypair import Keypair

from solbot.app import App
from solbot.config import Config
from solbot.settings import MODE_LIVE
from solbot.wallet import Wallet

from .fakechain import FakeChain

FIX = Path(__file__).parent / "fixtures"


def make_config(tmp_path: Path, **kw) -> Config:
    c = Config()
    c.db_path = tmp_path / "bot.sqlite3"
    c.rpc_urls = ["https://rpc1.test", "https://rpc2.test"]
    c.sim_rpc_url = "https://rpc1.test"
    c.bundle_sim_rpc_url = "https://simbundle.test"
    c.verify_rpc_a_url = "https://va.test"
    c.verify_rpc_b_url = "https://vb.test"
    c.jito_url = "https://tokyo.mainnet.block-engine.test"
    c.jupiter_base_url = "https://jup.test/swap/v1"
    c.poll_interval_ms = 25
    c.verify_poll_ms = 15
    c.reconcile_interval_ms = 100
    c.allow_live = True
    c.rpc_timeout_ms = 1000
    for k, v in kw.items():
        setattr(c, k, v)
    return c


@pytest.fixture
def chain():
    return FakeChain((FIX / "fee_config_mainnet.b64").read_text().strip())


class Harness:
    def __init__(self, tmp_path: Path, chain: FakeChain, kp: Keypair):
        self.tmp_path = tmp_path
        self.chain = chain
        self.kp = kp
        self.apps: list[App] = []

    async def start(self, live: bool = True, **cfg) -> App:
        config = make_config(self.tmp_path, **cfg)
        app = App(config, wallet=Wallet(self.kp), transport=self.chain.transport(), enable_telegram=False)
        if live:
            app.settings.set_mode(MODE_LIVE, confirmed_by_user=True)
        await app.start()
        self.apps.append(app)
        return app

    async def stop(self, app: App) -> None:
        await app.stop()
        self.apps.remove(app)


@pytest.fixture
async def harness(tmp_path, chain):
    kp = Keypair()
    chain.fund(kp.pubkey(), 100 * 10**9)
    h = Harness(tmp_path, chain, kp)
    yield h
    for a in list(h.apps):
        try:
            await a.stop()
        except Exception:
            pass


async def wait_for(pred, timeout=5.0, interval=0.02):
    t = 0.0
    while t < timeout:
        r = pred()
        if r:
            return r
        await asyncio.sleep(interval)
        t += interval
    raise AssertionError("condition not met in time")
