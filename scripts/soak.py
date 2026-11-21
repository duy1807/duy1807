#!/usr/bin/env python3
"""Mainnet soak test of the running bot in NO_SEND (nothing is ever sent).

    python scripts/soak.py start  --db data/soak.sqlite3 --seconds 120   # arms orders on live tokens, runs
    python scripts/soak.py resume --db data/soak.sqlite3 --seconds 60    # after a crash / kill -9

`start` discovers live PumpSwap tokens and arms, per token: LIMIT far below market (stays ARMED, used to
check restart recovery), LIMIT above market (fires immediately), DIP 0.3 % (fires on a real dip).
The wallet is whatever WALLET_* points to (an empty test wallet is fine: fired orders then stop at the
balance check, which is the expected NO_SEND behaviour). Every 10 s it prints RPC load, 429s, snapshot
freshness and order states; the summary is appended to reports/soak.md.
"""
import argparse
import asyncio
import json
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solders.pubkey import Pubkey  # noqa: E402

from solbot.app import App  # noqa: E402
from solbot.config import Config  # noqa: E402
from solbot.logging_setup import setup_logging  # noqa: E402
from solbot.orders import OrderParams  # noqa: E402


async def discover(app: App, n: int) -> list[str]:
    import importlib.util

    spec = importlib.util.spec_from_file_location("probe", Path(__file__).parent / "mainnet_probe.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    found = []
    for _ in range(4):
        mint, _ = await probe.discover(app)
        if str(mint) not in found:
            found.append(str(mint))
        if len(found) >= n:
            break
    return found


def params(count, **kw):
    base = dict(sol_per_buy=Decimal("0.01"), count=count, slippage_pct=Decimal(5), tp_pct=Decimal(20),
                sl_pct=Decimal(10))
    base.update(kw)
    return OrderParams(**base)


def rpc_totals(app: App) -> dict:
    tot: dict[str, list[int]] = {}
    for e in app.all_rpc_endpoints():
        t = tot.setdefault(e.name, [0, 0])
        t[0] += e.calls
        t[1] += e.rate_limited
    return {k: {"calls": v[0], "429": v[1]} for k, v in tot.items()}


async def run(mode: str, db: str, seconds: int, tokens: int, extra_limit: bool = False) -> None:
    setup_logging("WARNING")
    cfg = Config.from_env()
    cfg.allow_live = False
    cfg.db_path = Path(db)
    app = App(cfg, enable_telegram=False, enable_background=True)
    assert app.settings.mode == "NO_SEND"
    t_boot = time.monotonic()
    await app.start()
    boot_ms = (time.monotonic() - t_boot) * 1000
    out = {"mode": mode, "boot_ms": round(boot_ms), "recovery": app.recovery_report}
    print(json.dumps(out, default=str), flush=True)
    if mode == "start":
        mints = await discover(app, tokens)
        for mint in mints:
            snap = await app.market.fresh_snapshot(Pubkey.from_string(mint))
            p = snap.price_sol
            await app.orders.create("LIMIT", mint, params(3, limit_price=p * Decimal("0.5")), None)
            await app.orders.create("LIMIT", mint, params(1, limit_price=p * Decimal("1.003")), None)
            await app.orders.create("DIP", mint, params(5, dip_pct=Decimal("0.3")), None)
        print(f"armed on {mints}", flush=True)
    if extra_limit:
        mint = app.db.one("SELECT mint FROM orders ORDER BY id LIMIT 1")["mint"]
        snap = await app.market.fresh_snapshot(Pubkey.from_string(mint))
        for count in (1, 3, 5):  # 5 % above market: fires at once on live data
            await app.orders.create("LIMIT", mint, params(count, limit_price=snap.price_sol * Decimal("1.05")), None)
        print(f"extra LIMIT ×1/×3/×5 at +5% on {mint}", flush=True)
    ages: list[float] = []
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        await asyncio.sleep(1)
        for m in app.market.markets.values():
            if m.watchers and m.snapshot:
                ages.append(m.snapshot.age_ms())
        if int(t_end - time.monotonic()) % 10 == 0:
            eps = rpc_totals(app)
            st = {r["status"]: r["c"] for r in app.db.query("SELECT status, COUNT(*) c FROM orders GROUP BY status")}
            print(json.dumps({"t": round(seconds - (t_end - time.monotonic())), "orders": st, "poll": app.market.poll_stats,
                              "rpc": eps}), flush=True)
    elapsed = seconds
    fresh = sum(1 for a in ages if a <= 600)
    totals = rpc_totals(app)
    total_calls = sum(v["calls"] for v in totals.values())
    orders = app.db.query("SELECT id, kind, count, status, trigger_price, reference_price, status_reason, wait_note,"
                          " armed_at, triggered_at FROM orders ORDER BY id")
    lat = [1000 * (o["triggered_at"] - o["armed_at"]) for o in orders if o["triggered_at"] and o["armed_at"]]
    summary = {
        "mode": mode, "seconds": elapsed, "boot_ms": round(boot_ms),
        "snapshots_fresh_pct": round(100 * fresh / len(ages), 1) if ages else None,
        "snapshot_age_p50_ms": round(statistics.median(ages), 1) if ages else None,
        "snapshot_age_max_ms": round(max(ages), 1) if ages else None,
        "rpc_calls_per_s": round(total_calls / elapsed, 2),
        "rpc": totals,
        "poll": app.market.poll_stats,
        "orders": [{k: (str(v)[:90] if v is not None else None) for k, v in o.items()} for o in orders],
        "armed_to_trigger_ms": [round(x) for x in lat],
    }
    await app.stop()
    print("SUMMARY " + json.dumps(summary, default=str), flush=True)
    Path("reports").mkdir(exist_ok=True)
    with open("reports/soak.md", "a") as f:
        f.write(f"\n## soak {mode} {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n\n```json\n"
                f"{json.dumps(summary, indent=1, default=str)}\n```\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["start", "resume"])
    ap.add_argument("--db", default="data/soak.sqlite3")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--tokens", type=int, default=2)
    ap.add_argument("--extra-limit", action="store_true")
    a = ap.parse_args()
    asyncio.run(run(a.mode, a.db, a.seconds, a.tokens, a.extra_limit))


if __name__ == "__main__":
    main()
