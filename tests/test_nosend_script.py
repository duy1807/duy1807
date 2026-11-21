"""The mainnet NO_SEND acceptance script, exercised against the fake chain."""
import importlib.util
from decimal import Decimal
from pathlib import Path

spec = importlib.util.spec_from_file_location("nosend_check", Path(__file__).parent.parent / "scripts/nosend_check.py")
nc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nc)


async def test_nosend_check_script_all_pass(harness):
    app = await harness.start(live=False)
    nc.rows.clear()
    await nc.check_infra(app, app.config)
    await nc.check_grpc(app, harness.chain.mint)
    await nc.check_orders(app, str(harness.chain.mint), Decimal("0.01"))
    fails = [r for r in nc.rows if r[1] == "FAIL"]
    assert not fails, fails
    names = [r[0] for r in nc.rows]
    for c in (1, 3, 5):
        assert f"BUY NGAY ×{c} (NO_SEND)" in names and f"DIP ×{c} ARM" in names
    assert not [c for c in harness.chain.jito_calls if c["method"] in ("sendTransaction", "sendBundle")]
