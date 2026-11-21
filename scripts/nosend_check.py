#!/usr/bin/env python3
"""Mainnet NO_SEND acceptance check — run on the Tokyo VPS before enabling LIVE.

Nothing is ever sent: the app is forced into NO_SEND, Jito is only asked for tip accounts, and a
separate throw-away database is used.

    python scripts/nosend_check.py --mint <PUMPSWAP_TOKEN_MINT> [--sol 0.01] [--report reports/nosend.md]

Checks (PASS/FAIL):
  infra: every RPC (slot, latency, lag), verify RPC A/B, simulateBundle support, Jito getTipAccounts,
         Yellowstone gRPC stream (slots + pool account updates), Jupiter quote
  market: pool resolution + decode, fee tier, price/MC/liquidity, snapshot freshness
  orders: BUY NGAY ×1/×3/×5 and LIMIT ×1/×3/×5 (limit above market => immediate trigger) are planned,
          signed and simulated on mainnet state (×1 simulateTransaction, ×3/×5 simulateBundle);
          DIP ×1/×3/×5 are ARMED with locked reference/trigger prices and planned against a ceiling.
The wallet needs enough SOL for the simulated buys to succeed (simulation uses the real balance).
"""
import argparse
import asyncio
import os
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solders.pubkey import Pubkey  # noqa: E402

from solbot.app import App  # noqa: E402
from solbot.config import Config  # noqa: E402
from solbot.logging_setup import setup_logging  # noqa: E402
from solbot.orders import OrderParams  # noqa: E402
from solbot.rpc import RpcMethodUnsupported, RpcError  # noqa: E402

rows: list[tuple[str, str, str]] = []


def rec(name: str, ok: bool | None, detail: str) -> None:
    status = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
    rows.append((name, status, detail))
    print(f"[{status}] {name}: {detail}")


async def timed(coro):
    t = time.monotonic()
    r = await coro
    return r, (time.monotonic() - t) * 1000


async def check_infra(app: App, cfg: Config) -> None:
    slots = []
    for ep in app.rpc.endpoints:
        try:
            s, ms = await timed(ep.call("getSlot", [{"commitment": "processed"}]))
            slots.append(s)
            rec(f"RPC {ep.name}", True, f"slot {s}, {ms:.0f} ms")
        except Exception as e:
            rec(f"RPC {ep.name}", False, str(e))
    if len(slots) > 1:
        rec("RPC slot lag", max(slots) - min(slots) <= cfg.rpc_max_slot_lag, f"spread {max(slots) - min(slots)} slots")
    for label, pool in (("Verify RPC A", app.rpc_a), ("Verify RPC B", app.rpc_b)):
        try:
            h, ms = await timed(pool.get_block_height())
            rec(label, True, f"block height {h}, {ms:.0f} ms")
        except Exception as e:
            rec(label, False, str(e))
    rec("Verify RPCs independent", app.verify_independent,
        "two different endpoints" if app.verify_independent else "VERIFY_RPC_A_URL/B_URL not two distinct endpoints")
    if app.bundle_sim_rpc is None:
        rec("simulateBundle", False, "BUNDLE_SIM_RPC_URL not set — LIVE ×3/×5 will be refused")
    else:
        try:
            await app.bundle_sim_rpc.call("simulateBundle", [{"encodedTransactions": []}])
            rec("simulateBundle", True, "method available")
        except RpcMethodUnsupported as e:
            rec("simulateBundle", False, f"not supported: {e}")
        except RpcError as e:
            rec("simulateBundle", True, f"method available (rejected empty bundle: {e.message[:80]})")
        except Exception as e:
            rec("simulateBundle", False, str(e))
    try:
        await app.jito.refresh_tip_accounts()
        rec("Jito getTipAccounts", True, f"{app.jito.urls[0]} -> {len(app.jito.tip_accounts)} tip accounts")
    except Exception as e:
        rec("Jito getTipAccounts", False, str(e))
    try:
        from solbot.constants import WSOL_MINT

        q, ms = await timed(app.jupiter.quote(WSOL_MINT, Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
                                              10_000_000, 50))
        rec("Jupiter quote", True, f"0.01 SOL -> {q.out_amount} USDC raw, {ms:.0f} ms")
    except Exception as e:
        rec("Jupiter quote", None if not cfg.jupiter_base_url else False, str(e))


async def check_grpc(app: App, mint: Pubkey) -> None:
    if app.geyser is None:
        rec("Yellowstone gRPC", None, "GRPC_ENDPOINT not set — HTTP polling fallback in use")
        return
    app.geyser.start()
    m = await app.market.watch(mint, "nosend-check")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 8 and not app.geyser.connected:
        await asyncio.sleep(0.1)
    rec("gRPC connect", app.geyser.connected, app.config.grpc_endpoint.split(":")[0])
    v0 = m.version
    ages = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10:
        await asyncio.sleep(0.2)
        if m.snapshot:
            ages.append(m.snapshot.age_ms())
    fresh = sum(1 for a in ages if a <= app.config.snapshot_max_age_ms)
    rec("gRPC freshness", bool(ages) and fresh / len(ages) > 0.9,
        f"{fresh}/{len(ages)} samples ≤ {app.config.snapshot_max_age_ms} ms; pool updates seen: {m.version - v0}")
    app.market.unwatch(mint, "nosend-check")


async def check_orders(app: App, mint_s: str, sol: Decimal) -> None:
    mint = Pubkey.from_string(mint_s)
    try:
        m = await app.market.resolve(mint)
        snap = await app.market.fresh_snapshot(mint)
    except Exception as e:
        rec("Resolve token", False, str(e))
        return
    info = m.info
    detail = f"{info.name or '?'} ({info.symbol or '?'}), decimals {info.decimals}, route {m.route}"
    if m.pool:
        detail += f", pool {m.pool.address} canonical={m.pool.is_pump_pool} v2={m.pool.supports_v2}"
    rec("Resolve token", True, detail)
    if snap.ps_state:
        st = snap.ps_state
        rec("Pool snapshot", True, f"price {snap.price_sol} SOL/token, MC {st.market_cap_sol():.2f} SOL, "
                                   f"liquidity {st.liquidity_sol():.2f} SOL, fees {st.fees()} , "
                                   f"age {snap.age_ms():.0f} ms, slot {snap.slot}")
    base = dict(sol_per_buy=sol, slippage_pct=Decimal(10), tp_pct=Decimal(0), sl_pct=Decimal(0))
    for count in (1, 3, 5):
        o, res = await app.orders.create_and_wait("NOW", mint_s, OrderParams(count=count, **base), None)
        sub = app.db.get("submissions", res.submission_id) if res and res.submission_id else None
        ok = res is not None and res.status == "DRY_RUN" and sub is not None and sub["sim_ok"] == 1
        rec(f"BUY NGAY ×{count} (NO_SEND)", ok, f"{res.status if res else '?'}: "
            f"{(sub or {}).get('sim_detail') or (app.db.get('orders', o['id'])['status_reason'] or '')}")
    for count in (1, 3, 5):
        lim = (snap.price_sol * Decimal("1.05"))
        o = await app.orders.create("LIMIT", mint_s, OrderParams(count=count, limit_price=lim, **base), None)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 10:
            o = app.db.get("orders", o["id"])
            if o["status"] not in ("ARMED", "TRIGGERED"):
                break
            await asyncio.sleep(0.1)
        sub = app.db.get("submissions", o["submission_id"]) if o["submission_id"] else None
        rec(f"LIMIT ×{count} (NO_SEND, limit = giá×1.05)", o["status"] == "DRY_RUN" and sub and sub["sim_ok"] == 1,
            f"{o['status']}: {(sub or {}).get('sim_detail') or o['status_reason'] or o['wait_note']}")
    for count in (1, 3, 5):
        o = await app.orders.create("DIP", mint_s, OrderParams(count=count, dip_pct=Decimal(20), **base), None)
        ref, trig = Decimal(o["reference_price"]), Decimal(o["trigger_price"])
        ok = o["status"] == "ARMED" and trig == (ref * Decimal("0.8")).normalize()
        try:
            await app.engine.plan_buy(app.db.get("orders", o["id"]), snap)
            plan_note = "plan at current price unexpectedly within ceiling"
        except Exception as e:
            plan_note = f"ceiling enforced: {type(e).__name__}"
        app.orders.cancel(o["id"])
        rec(f"DIP ×{count} ARM", ok, f"ref {ref} -> trigger {trig}; {plan_note}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mint", required=True)
    ap.add_argument("--sol", default="0.01")
    ap.add_argument("--report", default="reports/nosend_check.md")
    a = ap.parse_args()
    setup_logging("WARNING")
    cfg = Config.from_env()
    cfg.allow_live = False  # hard NO_SEND for this script
    cfg.db_path = Path(tempfile.mkdtemp()) / "nosend_check.sqlite3"
    app = App(cfg, enable_telegram=False, enable_background=False)
    assert app.settings.mode == "NO_SEND"
    await app.start()
    app.blockhash.start()
    app.triggers.start()
    try:
        await check_infra(app, cfg)
        await check_grpc(app, Pubkey.from_string(a.mint))
        await check_orders(app, a.mint, Decimal(a.sol))
    finally:
        await app.stop()
    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    lines = ["# NO_SEND mainnet check", "", f"Wallet: `{app.wallet.pubkey}`  ", f"Mint: `{a.mint}`  ",
             f"Time: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}", "", "| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {n} | {s} | {d.replace('|', '/')} |" for n, s, d in rows]
    fails = sum(1 for _, s, _ in rows if s == "FAIL")
    lines += ["", f"**{len(rows) - fails}/{len(rows)} PASS/SKIP, {fails} FAIL**"]
    Path(a.report).write_text("\n".join(lines) + "\n")
    print(f"\nReport: {a.report} — {fails} FAIL")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONASYNCIODEBUG", "0")
    asyncio.run(main())
