"""Simulation Engine.

* ×1: ``simulateTransaction`` (signature verified, real blockhash, processed bank) + post-state of the
  user's token account.
* ×3/×5: ``simulateBundle`` (Jito-Solana compatible RPC) — transactions are executed *sequentially*
  on one bank, so every leg sees the pool/balances left by the previous legs.
  Simulating each transaction independently against the same initial state is NOT accepted as proof
  for a bundle; if no sequential method is available the bundle is refused.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from typing import Sequence

from solders.pubkey import Pubkey

from .rpc import HttpJsonRpc, RpcError, RpcMethodUnsupported, RpcTransportError, token_account_amount
from .tx_builder import BuiltTx

log = logging.getLogger("solbot.sim")

METHOD_SINGLE = "simulateTransaction"
METHOD_BUNDLE = "simulateBundle"
METHOD_NONE = "unavailable"


@dataclass
class SimExpect:
    """Post-state assertion: SPL token ``account`` must hold at least ``min_amount`` after this tx."""

    account: Pubkey
    min_amount: int


@dataclass
class SimResult:
    ok: bool
    method: str
    err: str | None = None
    per_tx: list[dict] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def summary(self) -> str:
        if self.ok:
            units = [t.get("units") for t in self.per_tx]
            return f"{self.method} OK units={units}"
        return f"{self.method} FAIL: {self.err}"


def _logs_tail(logs: list | None, n: int = 6) -> list[str]:
    return [str(x)[:200] for x in (logs or [])[-n:]]


class SimulationUnavailable(RpcTransportError):
    """No simulation endpoint answered (429 / timeout / connection): an infrastructure problem, not a
    verdict on the transaction. Nothing is sent; DIP/LIMIT orders go back to ARMED."""


class Simulator:
    def __init__(self, single_rpc: HttpJsonRpc | list[HttpJsonRpc], bundle_rpc: HttpJsonRpc | None):
        self.single_rpcs = list(single_rpc) if isinstance(single_rpc, (list, tuple)) else [single_rpc]
        self.single_rpc = self.single_rpcs[0]
        self.bundle_rpc = bundle_rpc
        self.bundle_supported: bool | None = None if bundle_rpc else False

    @property
    def can_simulate_bundles(self) -> bool:
        return self.bundle_rpc is not None and self.bundle_supported is not False

    async def simulate(self, txs: Sequence[BuiltTx], expects: Sequence[SimExpect | None],
                       min_slot: int | None = None) -> SimResult:
        import time

        t0 = time.monotonic()
        if len(txs) == 1:
            res = await self._single(txs[0], expects[0] if expects else None, min_slot)
        else:
            res = await self._bundle(txs, expects)
        res.elapsed_ms = (time.monotonic() - t0) * 1000
        return res

    async def _single(self, tx: BuiltTx, expect: SimExpect | None, min_slot: int | None = None) -> SimResult:
        cfg = {"encoding": "base64", "commitment": "processed", "sigVerify": True,
               "replaceRecentBlockhash": False}
        if min_slot:
            # never simulate on a bank older than the snapshot the transaction was planned on
            cfg["minContextSlot"] = int(min_slot)
        if expect:
            cfg["accounts"] = {"encoding": "base64", "addresses": [str(expect.account)]}
        r = await self._call_failover(self.single_rpcs, METHOD_SINGLE, [base64.b64encode(tx.raw).decode(), cfg])
        if isinstance(r, SimResult):
            return r
        v = (r or {}).get("value") or {}
        info = {"sig": tx.signature, "err": v.get("err"), "units": v.get("unitsConsumed"),
                "logs": _logs_tail(v.get("logs"))}
        if v.get("err") is not None:
            return SimResult(False, METHOD_SINGLE, f"tx error: {v.get('err')}", [info])
        problem = self._check_post(v.get("accounts"), expect, info)
        if problem:
            return SimResult(False, METHOD_SINGLE, problem, [info])
        if info["units"] is not None and info["units"] > tx.cu_limit > 0:
            return SimResult(False, METHOD_SINGLE, f"units {info['units']} > CU limit {tx.cu_limit}", [info])
        return SimResult(True, METHOD_SINGLE, None, [info])

    async def _call_failover(self, eps: list[HttpJsonRpc], method: str, params: list):
        """Try each endpoint; 429/transport errors move on (one short back-off pass). A deterministic
        JSON-RPC error is returned as a failed SimResult; total unavailability raises SimulationUnavailable."""
        import asyncio
        import time as _t

        from .rpc import RpcRateLimited

        last = ""
        for rnd in range(3):
            for ep in eps:
                if ep.rate_limited_until > _t.monotonic() and rnd == 0 and len(eps) > 1:
                    continue
                try:
                    return await ep.call(method, params)
                except RpcMethodUnsupported:
                    raise
                except RpcError as e:
                    if e.code == -32016:  # node behind minContextSlot: try another node / again shortly
                        last = f"{ep.name}: behind snapshot slot"
                        await asyncio.sleep(0.05)
                        continue
                    return SimResult(False, method, f"simulation RPC error: {e}")
                except RpcRateLimited as e:
                    last = f"{ep.name}: 429"
                    if len(eps) == 1:
                        await asyncio.sleep(min(e.retry_after or 0.2, 0.3))
                except RpcTransportError as e:
                    last = f"{ep.name}: {e}"
        raise SimulationUnavailable(f"không endpoint mô phỏng nào phản hồi ({last})")

    def _check_post(self, accounts, expect: SimExpect | None, info: dict) -> str | None:
        if not expect:
            return None
        if not accounts or accounts[0] is None:
            return "simulation returned no post-state for the token account"
        acc = accounts[0]
        data = acc.get("data")
        raw = base64.b64decode(data[0]) if isinstance(data, list) else b""
        try:
            amt = token_account_amount(raw)
        except ValueError:
            return "post-state token account undecodable"
        info["post_token_amount"] = amt
        if amt < expect.min_amount:
            return f"post token amount {amt} < required {expect.min_amount}"
        return None

    async def _bundle(self, txs: Sequence[BuiltTx], expects: Sequence[SimExpect | None]) -> SimResult:
        if self.bundle_rpc is None or self.bundle_supported is False:
            return SimResult(False, METHOD_NONE,
                             "không có phương pháp mô phỏng Bundle tuần tự (BUNDLE_SIM_RPC_URL) — từ chối gửi")
        post_cfgs = []
        for e in expects:
            post_cfgs.append({"addresses": [str(e.account)], "encoding": "base64"} if e else None)
        params = [
            {"encodedTransactions": [base64.b64encode(t.raw).decode() for t in txs]},
            {
                "skipSigVerify": False,
                "replaceRecentBlockhash": False,
                "transactionEncoding": "base64",
                "preExecutionAccountsConfigs": [None] * len(txs),
                "postExecutionAccountsConfigs": post_cfgs,
            },
        ]
        try:
            r = await self._call_failover([self.bundle_rpc], METHOD_BUNDLE, params)
        except RpcMethodUnsupported:
            self.bundle_supported = False
            return SimResult(False, METHOD_NONE, "RPC không hỗ trợ simulateBundle — từ chối gửi Bundle")
        if isinstance(r, SimResult):
            return r
        self.bundle_supported = True
        v = (r or {}).get("value") or {}
        summary = v.get("summary")
        results = v.get("transactionResults") or []
        per = []
        for i, t in enumerate(results):
            info = {"sig": txs[i].signature if i < len(txs) else None, "err": t.get("err"),
                    "units": t.get("unitsConsumed"), "logs": _logs_tail(t.get("logs"))}
            per.append(info)
        if summary != "succeeded":
            return SimResult(False, METHOD_BUNDLE, f"bundle simulation failed: {summary}", per)
        if len(results) != len(txs):
            return SimResult(False, METHOD_BUNDLE, f"simulateBundle returned {len(results)}/{len(txs)} results",
                             per)
        for i, (t, e) in enumerate(zip(results, expects)):
            if t.get("err") is not None:
                return SimResult(False, METHOD_BUNDLE, f"tx #{i + 1} error: {t.get('err')}", per)
            problem = self._check_post(t.get("postExecutionAccounts"), e, per[i])
            if problem:
                return SimResult(False, METHOD_BUNDLE, f"tx #{i + 1}: {problem}", per)
            units = per[i]["units"]
            if units is not None and txs[i].cu_limit and units > txs[i].cu_limit:
                return SimResult(False, METHOD_BUNDLE, f"tx #{i + 1} units {units} > CU limit", per)
        return SimResult(True, METHOD_BUNDLE, None, per)
