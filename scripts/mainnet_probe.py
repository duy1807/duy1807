#!/usr/bin/env python3
"""Mainnet probe — proves on live mainnet state that the bot's PumpSwap transactions are correct.

NOTHING IS SIGNED OR SENT. Transactions built by the bot's own code are run through
``simulateTransaction`` with ``sigVerify=false`` / ``replaceRecentBlockhash=true`` using a public,
funded address as the simulated fee payer (read-only; required because the bot's test wallet is empty).

Checks
  1. Discovery: a live canonical PumpSwap pool (WSOL quote, v2-capable) from recent program activity.
  2. BUY exactness: buy_v2 with max_quote_amount_in = cost computed by the bot -> succeeds and the payer
     receives exactly base_amount_out; with cost - 1 lamport -> rejected by the program.
     => account layout, PDAs, fee tier and rounding match the on-chain program to the lamport.
  3. Sequential ×3 legs (one tx, three buy_v2): each leg's max_in = the planner's sequential cost ->
     succeeds; leg 3 with cost - 1 -> rejected. => the planner's post-trade state model is exact.
  4. simulateBundle availability on BUNDLE_SIM_RPC_URL (and a real 3-tx bundle simulation if available).
  5. SELL exactness: sell_v2 with min_quote_amount_out = user_out computed by the bot -> succeeds;
     user_out + 1 -> rejected.
  6. DIP/LIMIT ceiling: planner refuses a ceiling just below the planned execution price; at the exact
     ceiling the capped max_in passes on-chain.
  7. Latency of every hot-path stage over warm connections (RPC, simulate, Jito RTT).

    python scripts/mainnet_probe.py [--mint MINT] [--report reports/mainnet_probe.md]
"""
import argparse
import asyncio
import base64
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from solbot import constants as C  # noqa: E402
from solbot.app import App  # noqa: E402
from solbot.config import Config  # noqa: E402
from solbot.dex import pumpswap as ps  # noqa: E402
from solbot.engine import PriceCapNotMet, cap_lamports  # noqa: E402
from solbot.logging_setup import setup_logging  # noqa: E402
from solbot.rpc import RpcError, RpcMethodUnsupported, token_account_amount  # noqa: E402
from solbot.tx_builder import ata, compute_budget  # noqa: E402
from solbot.wallet import Wallet  # noqa: E402

TRADE_DISCS = {bytes(C.IX_BUY_V2), bytes(C.IX_SELL_V2), bytes([102, 6, 61, 18, 1, 218, 235, 234]),
               bytes([51, 230, 133, 164, 1, 127, 131, 173]), bytes([198, 46, 21, 82, 180, 217, 232, 112])}

rows: list[tuple[str, str, str]] = []
lat: dict[str, list[float]] = {}


def rec(name, ok, detail):
    st = "PASS" if ok else ("INFO" if ok is None else "FAIL")
    rows.append((name, st, detail))
    print(f"[{st}] {name}: {detail}", flush=True)


async def t(name, coro):
    t0 = time.perf_counter()
    r = await coro
    lat.setdefault(name, []).append((time.perf_counter() - t0) * 1000)
    return r


def unsigned_tx(payer: Pubkey, ixs) -> str:
    msg = MessageV0.try_compile(payer, ixs, [], Hash.default())
    tx = VersionedTransaction.populate(msg, [Signature.default()] * msg.header.num_required_signatures)
    return base64.b64encode(bytes(tx)).decode()


async def simulate(app: App, payer: Pubkey, ixs, watch: list[Pubkey]) -> dict:
    cfg = {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "processed",
           "accounts": {"encoding": "base64", "addresses": [str(k) for k in watch]}}
    if app.rpc.best_slot:
        cfg["minContextSlot"] = app.rpc.best_slot
    r = await t("simulateTransaction", sim_call(app, "simulateTransaction", [unsigned_tx(payer, ixs), cfg]))
    v = r["value"]
    amounts = []
    for a in v.get("accounts") or []:
        amounts.append(token_account_amount(base64.b64decode(a["data"][0])) if a else 0)
    return {"err": v.get("err"), "units": v.get("unitsConsumed"), "amounts": amounts,
            "logs": (v.get("logs") or [])[-4:], "slot": r["context"]["slot"]}


async def sim_call(app: App, method: str, params: list, eps=None):
    """Probe-side retry: try every simulate endpoint, back off on 429 (free tiers are rate limited)."""
    from solbot.rpc import RpcError, RpcRateLimited, RpcTransportError

    eps = eps or app.sim_rpcs
    for attempt in range(8):
        for ep in eps:
            try:
                return await ep.call(method, params)
            except RpcRateLimited as e:
                lat.setdefault("429 responses", []).append(0)
                await asyncio.sleep(min(e.retry_after or 0.5, 1.0))
            except RpcTransportError:
                await asyncio.sleep(0.2)
            except RpcError as e:
                if e.code != -32016:  # -32016: node behind minContextSlot -> retry
                    raise
                lat.setdefault("node behind snapshot slot (-32016)", []).append(0)
                await asyncio.sleep(0.1)
    raise SystemExit(f"{method}: all simulate endpoints rate limited / unavailable")


async def rpc_retry(app: App, method: str, params: list):
    """Probe-side read with back-off (free-tier endpoints rate-limit heavy methods)."""
    from solbot.rpc import RpcRateLimited, RpcTransportError

    for attempt in range(8):
        try:
            return await app.rpc.call(method, params, stale_check=False)
        except (RpcRateLimited, RpcTransportError):
            lat.setdefault("429 responses", []).append(0)
            await asyncio.sleep(1.0 + attempt * 0.5)
    raise SystemExit(f"{method}: rate limited on every endpoint")


async def discover(app: App) -> tuple[Pubkey, list[Pubkey]]:
    sigs = await rpc_retry(app, "getSignaturesForAddress", [str(C.PUMP_AMM_PROGRAM), {"limit": 40}])
    counts: dict[str, int] = {}
    users: dict[str, list[str]] = {}
    for s in sigs:
        if s.get("err"):
            continue
        try:
            tx = await app.rpc.get_transaction(s["signature"])  # v0 only; newer tx versions are skipped
        except RpcError:
            continue
        if not tx:
            continue
        msg = tx["transaction"]["message"]
        keys = list(msg["accountKeys"]) + list((tx["meta"].get("loadedAddresses") or {}).get("writable") or []) \
            + list((tx["meta"].get("loadedAddresses") or {}).get("readonly") or [])
        ixs = list(msg["instructions"])
        for inner in tx["meta"].get("innerInstructions") or []:
            ixs += inner["instructions"]
        import base58

        for ix in ixs:
            if keys[ix["programIdIndex"]] != str(C.PUMP_AMM_PROGRAM):
                continue
            data = base58.b58decode(ix["data"])
            if data[:8] in TRADE_DISCS and len(ix["accounts"]) >= 9:
                mint = keys[ix["accounts"][3]]
                quote = keys[ix["accounts"][4]]
                if quote == str(C.WSOL_MINT):
                    counts[mint] = counts.get(mint, 0) + 1
                    users.setdefault(mint, []).append(keys[ix["accounts"][1]])
        if len(counts) >= 6:
            break
    # least active first: exactness checks need the pool to stay still between snapshot and simulation
    for mint, _ in sorted(counts.items(), key=lambda kv: kv[1]):
        m = Pubkey.from_string(mint)
        acc = await app.rpc.get_account(C.canonical_pump_pool(m), "confirmed")
        if acc is not None:
            pool = ps.decode_pool(C.canonical_pump_pool(m), acc.data)
            if pool.supports_v2 and pool.quote_mint == C.WSOL_MINT:
                everyone = [u for us in users.values() for u in us]  # any funded trader can be the payer
                return m, [Pubkey.from_string(u) for u in dict.fromkeys(users[mint] + everyone)]
    raise SystemExit("no live canonical v2 PumpSwap pool found in recent activity")


async def funded_payer(app: App, candidates: list[Pubkey], min_lamports: int) -> Pubkey:
    _, accs = await app.rpc.get_multiple_accounts(candidates, "processed")
    for k, a in zip(candidates, accs):
        if a and a.owner == C.SYSTEM_PROGRAM and a.lamports >= min_lamports and k.is_on_curve():
            return k
    raise SystemExit("no funded payer among recent traders")


async def holder(app: App, mint: Pubkey, token_program: Pubkey, exclude: set) -> tuple[Pubkey, int] | None:
    res = await rpc_retry(app, "getTokenLargestAccounts", [str(mint), {"commitment": "confirmed"}])
    addrs = [Pubkey.from_string(v["address"]) for v in res["value"][:20]]
    _, accs = await app.rpc.get_multiple_accounts(addrs, "processed")
    owners = []
    for a in accs:
        if a and len(a.data) >= 72:
            owner = Pubkey.from_bytes(a.data[32:64])
            amt = token_account_amount(a.data)
            if owner not in exclude and owner.is_on_curve() and amt > 0:
                owners.append((owner, amt, a.pubkey))
    if not owners:
        return None
    _, oaccs = await app.rpc.get_multiple_accounts([o[0] for o in owners], "processed")
    for (owner, amt, tacc), oa in zip(owners, oaccs):
        if oa and oa.lamports >= 10_000_000 and tacc == ata(owner, mint, token_program):
            return owner, amt
    return None


async def fresh(app: App, mint: Pubkey):
    m = app.market.markets[mint]
    return await t("snapshot(getMultipleAccounts)", app.market.fetch_now(m))


async def check_buy_exact(app, mint, payer) -> None:
    """Simulate max_in = cost and max_in = cost-1 *concurrently* on the same snapshot; accept a verdict only
    when the passing simulation proves the pool state was unchanged. If cost-1 also passes on an unchanged
    state, binary-search the smallest accepted max_in and report the difference."""
    eng = app.engine
    for attempt in range(12):
        snap = await fresh(app, mint)
        st = snap.ps_state
        plan = eng.plan_pumpswap_buy(snap, 1, 10_000_000, 0, None, 200_000)
        leg = plan.legs[0]
        cost = leg.spend_lamports
        user_ata = ata(payer, mint, plan.token_program)
        _, (pre,) = await app.rpc.get_multiple_accounts([user_ata], "processed")
        pre_amt = token_account_amount(pre.data) if pre else 0
        watch = [user_ata, st.pool.pool_base_token_account]

        def ixs(max_in):
            leg.max_in = max_in
            return compute_budget(200_000, 0) + eng.pumpswap_buy_ixs(plan, leg, payer)

        ok, bad = await asyncio.gather(simulate(app, payer, ixs(cost), watch), simulate(app, payer, ixs(cost - 1), watch))
        expect_vault = st.base_reserve - leg.base_out
        if not (ok["err"] is None and ok["amounts"][1] == expect_vault):
            continue  # state moved between snapshot and simulation
        if bad["err"] is not None:
            rec("BUY exact cost accepted on-chain", ok["amounts"][0] - pre_amt == leg.base_out,
                f"0.01 SOL -> base_out {leg.base_out}, cost {cost} lamports (fees {st.fees()}), received "
                f"{ok['amounts'][0] - pre_amt}, units {ok['units']}, attempt {attempt + 1}")
            rec("BUY cost-1 lamport rejected on-chain", True, f"err={bad['err']}")
            return
        if bad["amounts"] and bad["amounts"][1] == expect_vault:
            lo, hi = 0, cost - 1  # find the smallest max_in the program accepts on (hopefully) this state
            while hi - lo > 1:
                mid = (lo + hi) // 2
                r = await simulate(app, payer, ixs(mid), watch)
                if r["err"] is None:
                    hi = mid
                else:
                    lo = mid
            rec("BUY exact cost accepted on-chain", True, f"cost {cost}, received {ok['amounts'][0] - pre_amt}")
            rec("BUY cost-1 lamport rejected on-chain", False,
                f"program accepted {cost - 1}; smallest accepted ≈ {hi} -> bot over-estimates by {cost - hi} lamports "
                f"(safe direction, but not exact)")
            return
    rec("BUY exact cost accepted on-chain", False, "pool kept changing between snapshot and simulation (12 tries)")


async def check_sequential(app, mint, payer) -> None:
    from solbot.tx_builder import sol_transfer, sync_native, create_ata_idempotent, wsol_unwrap

    eng = app.engine
    for attempt in range(12):
        snap = await fresh(app, mint)
        st = snap.ps_state
        plan = eng.plan_pumpswap_buy(snap, 3, 10_000_000, 0, None, 400_000)
        for leg in plan.legs:
            leg.max_in = leg.spend_lamports
        user_wsol = ata(payer, C.WSOL_MINT)
        user_base = ata(payer, mint, plan.token_program)

        def ixs(last_minus=0):
            out = compute_budget(400_000, 0) + [create_ata_idempotent(payer, payer, mint, plan.token_program),
                                                create_ata_idempotent(payer, payer, C.WSOL_MINT),
                                                sol_transfer(payer, user_wsol, sum(l.max_in for l in plan.legs)),
                                                sync_native(user_wsol)]
            for leg in plan.legs:
                mi = leg.max_in - (last_minus if leg.idx == 2 else 0)
                out.append(ps.build_buy_v2(payer, st.pool, plan.token_program, C.TOKEN_PROGRAM, user_base, user_wsol,
                                           eng._buyback_ata(), leg.base_out, mi))
            return out + [wsol_unwrap(payer)]

        _, (pre,) = await app.rpc.get_multiple_accounts([user_base], "processed")
        pre_amt = token_account_amount(pre.data) if pre else 0
        ok = await simulate(app, payer, ixs(), [user_base, st.pool.pool_base_token_account])
        tot = sum(l.base_out for l in plan.legs)
        if not (ok["err"] is None and ok["amounts"][1] == st.base_reserve - tot):
            if ok["err"] is not None and attempt == 11:
                rec("Sequential ×3 exact costs", False, f"err={ok['err']} {ok['logs']}")
            continue
        bad = await simulate(app, payer, ixs(1), [user_base])
        costs = [l.spend_lamports for l in plan.legs]
        rec("Sequential ×3 exact costs accepted on-chain", ok["amounts"][0] - pre_amt == tot,
            f"legs base_out {[l.base_out for l in plan.legs]}, costs {costs} (rising price), units {ok['units']}")
        rec("Sequential ×3 leg-3 cost-1 rejected", bad["err"] is not None, f"err={bad['err']}")
        return


async def check_bundle_sim(app, mint, payer) -> None:
    if app.bundle_sim_rpc is None:
        rec("simulateBundle", False, "BUNDLE_SIM_RPC_URL not set")
        return
    snap = await fresh(app, mint)
    # realistic 5% slippage: exactness is proven separately; this checks sequential bundle execution
    plan = app.engine.plan_pumpswap_buy(snap, 3, 10_000_000, 500, None, 200_000)
    txs = [unsigned_tx(payer, compute_budget(200_000, 0) + app.engine.pumpswap_buy_ixs(plan, l, payer))
           for l in plan.legs]
    cfg = {"skipSigVerify": True, "replaceRecentBlockhash": True, "transactionEncoding": "base64",
           "preExecutionAccountsConfigs": [None] * 3, "postExecutionAccountsConfigs": [None] * 3}
    try:
        r = await t("simulateBundle", sim_call(app, "simulateBundle", [{"encodedTransactions": txs}, cfg],
                                               [app.bundle_sim_rpc]))
        rec("simulateBundle", r["value"]["summary"] == "succeeded", f"summary={r['value']['summary']}")
    except RpcMethodUnsupported as e:
        rec("simulateBundle", False, f"{app.bundle_sim_rpc.name} does not support simulateBundle ({e.message}) — "
                                     f"LIVE ×3/×5 will be refused until a Jito-compatible RPC is configured")
    except RpcError as e:
        rec("simulateBundle", False, f"RPC error: {e}")


async def check_sell_exact(app, mint, token_program, exclude) -> None:
    h = await holder(app, mint, token_program, exclude)
    if h is None:
        rec("SELL exact", None, "no suitable holder found")
        return
    seller, bal = h
    for attempt in range(6):
        snap = await fresh(app, mint)
        st = snap.ps_state
        amount = max(bal // 3, 1)
        out = st.quote_sell(amount)
        ok = await simulate(app, seller, compute_budget(200_000, 0) +
                            app.engine.pumpswap_sell_ixs(st, seller, token_program, amount, out.user_out),
                            [st.pool.pool_base_token_account])
        if not (ok["err"] is None and ok["amounts"][0] == st.base_reserve + amount):
            continue
        bad = await simulate(app, seller, compute_budget(200_000, 0) +
                             app.engine.pumpswap_sell_ixs(st, seller, token_program, amount, out.user_out + 1),
                             [st.pool.pool_base_token_account])
        rec("SELL exact user_out accepted on-chain", True,
            f"sell {amount} -> {out.user_out} lamports net of fees, units {ok['units']}, attempt {attempt + 1}")
        rec("SELL user_out+1 rejected on-chain", bad["err"] is not None, f"err={bad['err']}")
        return
    rec("SELL exact user_out accepted on-chain", False, "pool kept changing (6 tries)")


async def check_ceiling(app, mint, payer) -> None:
    eng = app.engine
    for attempt in range(6):
        snap = await fresh(app, mint)
        st = snap.ps_state
        p = eng.plan_pumpswap_buy(snap, 1, 10_000_000, 10_000, None, 200_000).legs[0]
        try:
            eng.plan_pumpswap_buy(snap, 1, 10_000_000, 10_000, p.planned_price * Decimal("0.999"), 200_000)
            refused = False
        except PriceCapNotMet:
            refused = True
        cap = p.planned_price * Decimal("1.0000001")
        plan = eng.plan_pumpswap_buy(snap, 1, 10_000_000, 10_000, cap, 200_000)  # slippage 100%
        leg = plan.legs[0]
        bound = cap_lamports(leg.base_out, cap, st.base_decimals)
        ok = await simulate(app, payer, compute_budget(200_000, 0) + eng.pumpswap_buy_ixs(plan, leg, payer),
                            [st.pool.pool_base_token_account])
        if ok["amounts"] and ok["amounts"][0] != st.base_reserve - leg.base_out:
            continue
        rec("DIP/LIMIT ceiling below planned price refused (nothing built)", refused,
            f"planned {p.planned_price:.6e} SOL/token, ceiling ×0.999")
        rec("Slippage 100% capped by ceiling on-chain", ok["err"] is None and leg.max_in <= bound < 20_000_000,
            f"max_in {leg.max_in} (100% slippage would allow 20000000), ceiling bound {bound}")
        return


async def check_latency(app, mint, payer) -> None:
    eng = app.engine
    m = app.market.markets[mint]
    for _ in range(3):  # warm all connections first
        await app.rpc.get_slot()
        await app.jito.refresh_tip_accounts()
    for i in range(10):
        await t("RPC getSlot (warm)", app.rpc.get_slot())
        await t("userAccounts(getMultipleAccounts)", eng._user_accounts(mint, m.info.token_program))
        await t("blockhash(cached)", app.blockhash.get())
        snap = await fresh(app, mint)
        for count in (1, 3, 5):
            t0 = time.perf_counter()
            plan = eng.plan_pumpswap_buy(snap, count, 10_000_000, 1000, None, 200_000)
            lat.setdefault(f"plan ×{count}", []).append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            await eng.build_buy_txs(plan, str(Hash.default()), 100_000, 10_000, app.jito.next_tip_account(),
                                    {"base_ata": ata(app.wallet.pubkey, mint, m.info.token_program)})
            lat.setdefault(f"build+sign ×{count}", []).append((time.perf_counter() - t0) * 1000)
        await simulate(app, payer, compute_budget(200_000, 0) +
                       eng.pumpswap_buy_ixs(eng.plan_pumpswap_buy(snap, 1, 10_000_000, 1000, None, 200_000),
                                            eng.plan_pumpswap_buy(snap, 1, 10_000_000, 1000, None, 200_000).legs[0],
                                            payer), [])
        t0 = time.perf_counter()
        await app.jito._post(app.jito.urls[0], "/api/v1/bundles", "getTipAccounts", [])
        lat.setdefault("Jito Tokyo RTT (warm)", []).append((time.perf_counter() - t0) * 1000)


async def check_pipeline(app, mint, payer, rounds: int = 8) -> None:
    """End-to-end hot path, step by step exactly as TradeEngine runs it, over warm connections:
    signal -> [snapshot + wallet state] -> plan -> build -> simulate(×1) / simulateBundle(×3/×5) -> Jito ACK.
    The simulated payer replaces the (empty) bot wallet; Jito RTT is measured with getTipAccounts on the
    bundle endpoint (sendBundle itself is never called)."""
    eng = app.engine
    m = app.market.markets[mint]
    await app.warm_connections()
    for kind in ("BUY NGAY", "DIP/LIMIT"):
        for count in (1, 3, 5):
            key = f"E2E {kind} ×{count} (signal→Jito ACK)"
            for _ in range(rounds):
                t0 = time.perf_counter()
                if kind == "BUY NGAY":
                    snap, _accts = await asyncio.gather(app.market.fetch_now(m),
                                                        eng._user_accounts(mint, m.info.token_program))
                else:
                    snap = m.snapshot  # delivered by the stream; wallet state prefetched while ARMED
                    await eng._user_accounts(mint, m.info.token_program, max_age_ms=2000)
                t_snap = time.perf_counter()
                plan = eng.plan_pumpswap_buy(snap, count, 10_000_000, 1000, None, 200_000)
                txs = [unsigned_tx(payer, compute_budget(200_000, 100_000) + eng.pumpswap_buy_ixs(plan, l, payer))
                       for l in plan.legs]
                t_build = time.perf_counter()
                if count == 1:
                    cfg = {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
                           "commitment": "processed"}
                    # the bot's own failover path (429 -> next endpoint immediately)
                    await app.simulator._call_failover(app.simulator.single_rpcs, "simulateTransaction", [txs[0], cfg])
                else:
                    cfg = {"skipSigVerify": True, "replaceRecentBlockhash": True, "transactionEncoding": "base64",
                           "preExecutionAccountsConfigs": [None] * count, "postExecutionAccountsConfigs": [None] * count}
                    await app.simulator._call_failover([app.bundle_sim_rpc], "simulateBundle",
                                                       [{"encodedTransactions": txs}, cfg])
                t_sim = time.perf_counter()
                await app.jito._post(app.jito.urls[0], "/api/v1/bundles", "getTipAccounts", [])
                t_end = time.perf_counter()
                lat.setdefault(key, []).append((t_end - t0) * 1000)
                lat.setdefault(f"  · {kind} ×{count}: data", []).append((t_snap - t0) * 1000)
                lat.setdefault(f"  · {kind} ×{count}: plan+build", []).append((t_build - t_snap) * 1000)
                lat.setdefault(f"  · {kind} ×{count}: simulate", []).append((t_sim - t_build) * 1000)
                lat.setdefault(f"  · {kind} ×{count}: Jito", []).append((t_end - t_sim) * 1000)
            v = lat[key]
            rec(key, statistics.median(v) < 500, f"p50 {statistics.median(v):.0f} ms, max {max(v):.0f} ms "
                                                 f"(target < 500 ms)")


def p50(v):
    return statistics.median(v)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mint")
    ap.add_argument("--report", default="reports/mainnet_probe.md")
    a = ap.parse_args()
    setup_logging("WARNING")
    cfg = Config.from_env()
    cfg.allow_live = False
    import tempfile

    cfg.db_path = Path(tempfile.mkdtemp()) / "probe.sqlite3"
    cfg.grpc_endpoint = None
    app = App(cfg, wallet=Wallet(Keypair()), enable_telegram=False, enable_background=False)
    await app.start()
    try:
        rec("Endpoints", None, str(cfg.describe()))
        if a.mint:
            mint, traders = Pubkey.from_string(a.mint), []
        else:
            mint, traders = await discover(app)
        m = await app.market.resolve(mint, "pumpswap")
        snap = await fresh(app, mint)
        st = snap.ps_state
        rec("Live PumpSwap pool decoded", True,
            f"mint {mint} pool {m.pool.address} token_program {'Token-2022' if m.info.token_program == C.TOKEN_2022_PROGRAM else 'SPL'}"
            f" price {snap.price_sol:.6e} SOL/token MC {st.market_cap_sol():.1f} SOL liq {st.liquidity_sol():.1f} SOL"
            f" fees {st.fees()} virtual_quote {st.pool.virtual_quote_reserves} slot {snap.slot}")
        payer = await funded_payer(app, traders or [], 80_000_000) if traders else None
        if payer is None:
            raise SystemExit("pass --mint of a pool with recent traders")
        rec("Simulated fee payer (public address, nothing signed)", None, str(payer))
        await check_buy_exact(app, mint, payer)
        await check_sequential(app, mint, payer)
        await check_bundle_sim(app, mint, payer)
        await check_sell_exact(app, mint, m.info.token_program, {payer, m.pool.address})
        await check_ceiling(app, mint, payer)
        await check_latency(app, mint, payer)
        await check_pipeline(app, mint, payer)
    finally:
        await app.stop()
    lines = ["# Mainnet probe (NO_SEND — nothing signed or sent)", "",
             f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}", "",
             "| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {n} | {s} | {d.replace('|', '/')} |" for n, s, d in rows]
    lines += ["", "## Latency (ms, warm connections, from the machine that ran this script)", "",
              "| Stage | p50 | min | max | n |", "|---|---|---|---|---|"]
    for k, v in lat.items():
        lines.append(f"| {k} | {p50(v):.1f} | {min(v):.1f} | {max(v):.1f} | {len(v)} |")
    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    Path(a.report).write_text("\n".join(lines) + "\n")
    print("\n".join(lines[-(len(lat) + 2):]))
    fails = sum(1 for r in rows if r[1] == "FAIL")
    print(f"\nreport: {a.report} — {fails} FAIL")


if __name__ == "__main__":
    asyncio.run(main())
