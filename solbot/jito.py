"""Jito Block Engine transport (Tokyo by default).

* ×1  -> ``sendTransaction`` (bundleOnly=true: revert-protected, tip included in the tx)
* ×3/×5 -> ``sendBundle`` (atomic; tip in the last tx)

An HTTP 200 with a result is an ACK — it is NOT success; landing is verified on-chain separately.
On 429 / connection errors the *identical signed bytes* may be re-sent a bounded number of times:
the same signatures can only ever execute once, so this never creates a duplicate trade.
There is intentionally no fallback to public RPC sendTransaction.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field

import httpx
from solders.pubkey import Pubkey

from . import constants as C

log = logging.getLogger("solbot.jito")


class JitoError(RuntimeError):
    pass


@dataclass
class JitoAck:
    ok: bool
    kind: str  # transaction | bundle
    endpoint: str
    ack_ms: float
    signature: str | None = None
    bundle_id: str | None = None
    http_status: int | None = None
    detail: str = ""
    attempts: int = 0
    fanout: list[dict] = field(default_factory=list)


def _host(u: str) -> str:
    return u.split("://", 1)[-1].split("/", 1)[0]


class JitoClient:
    def __init__(self, url: str, extra_urls: list[str] | None = None, uuid: str | None = None,
                 timeout_ms: int = 2000, identical_resend: int = 2, client: httpx.AsyncClient | None = None):
        self.urls = [url.rstrip("/")] + [u.rstrip("/") for u in (extra_urls or [])]
        self.uuid = uuid
        self.timeout = timeout_ms / 1000
        self.identical_resend = max(0, identical_resend)
        headers = {"content-type": "application/json"}
        if uuid:
            headers["x-jito-auth"] = uuid
        self._client = client or httpx.AsyncClient(timeout=self.timeout, headers=headers)
        self.tip_accounts: list[Pubkey] = list(C.JITO_TIP_ACCOUNTS)
        self._rr = 0

    def next_tip_account(self) -> Pubkey:
        acc = self.tip_accounts[self._rr % len(self.tip_accounts)]
        self._rr += 1
        return acc

    async def _post(self, base: str, path: str, method: str, params: list) -> tuple[int, dict | None, dict]:
        q = f"?uuid={self.uuid}" if self.uuid else ""
        if path.endswith("/transactions"):
            q = (q + "&" if q else "?") + "bundleOnly=true"
        r = await self._client.post(f"{base}{path}{q}",
                                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        body = None
        try:
            body = r.json()
        except ValueError:
            pass
        return r.status_code, body, dict(r.headers)

    async def _send_one(self, base: str, path: str, method: str, params: list, kind: str) -> JitoAck:
        t0 = time.monotonic()
        attempts = 0
        last = ""
        status = None
        while attempts <= self.identical_resend:
            attempts += 1
            try:
                status, body, headers = await self._post(base, path, method, params)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = f"{type(e).__name__}"
                await asyncio.sleep(0.05 * attempts)
                continue
            if status == 200 and body and body.get("result") is not None:
                res = body["result"]
                ack = JitoAck(True, kind, _host(base), (time.monotonic() - t0) * 1000, http_status=status,
                              attempts=attempts)
                if kind == "bundle":
                    ack.bundle_id = str(res)
                else:
                    ack.signature = str(res)
                    ack.bundle_id = headers.get("x-bundle-id")
                return ack
            if status == 429:
                last = "HTTP 429 rate limited"
                await asyncio.sleep(0.1 * attempts)
                continue
            err = (body or {}).get("error") if isinstance(body, dict) else None
            last = f"HTTP {status}: {err or ''}"[:300]
            break  # deterministic rejection: do not resend
        return JitoAck(False, kind, _host(base), (time.monotonic() - t0) * 1000, http_status=status,
                       detail=last, attempts=attempts)

    async def _fanout(self, path: str, method: str, params: list, kind: str) -> JitoAck:
        results = await asyncio.gather(*[self._send_one(u, path, method, params, kind) for u in self.urls])
        ok = [r for r in results if r.ok]
        best = min(ok, key=lambda r: r.ack_ms) if ok else results[0]
        best.fanout = [{"endpoint": r.endpoint, "ok": r.ok, "ms": round(r.ack_ms, 1), "detail": r.detail}
                       for r in results]
        return best

    async def send_transaction(self, raw: bytes) -> JitoAck:
        params = [base64.b64encode(raw).decode(), {"encoding": "base64"}]
        return await self._fanout("/api/v1/transactions", "sendTransaction", params, "transaction")

    async def send_bundle(self, raws: list[bytes]) -> JitoAck:
        if not 1 <= len(raws) <= C.JITO_MAX_BUNDLE_TXS:
            raise JitoError("bundle must contain 1..5 transactions")
        params = [[base64.b64encode(r).decode() for r in raws], {"encoding": "base64"}]
        return await self._fanout("/api/v1/bundles", "sendBundle", params, "bundle")

    async def refresh_tip_accounts(self) -> None:
        try:
            status, body, _ = await self._post(self.urls[0], "/api/v1/bundles", "getTipAccounts", [])
            if status == 200 and body and isinstance(body.get("result"), list) and body["result"]:
                self.tip_accounts = [Pubkey.from_string(x) for x in body["result"]]
        except Exception as e:
            log.warning("getTipAccounts failed, using built-in list: %s", e)

    async def inflight_bundle_status(self, bundle_id: str) -> str | None:
        try:
            status, body, _ = await self._post(self.urls[0], "/api/v1/bundles", "getInflightBundleStatuses",
                                               [[bundle_id]])
            if status == 200 and body and body.get("result"):
                vals = body["result"].get("value") or []
                if vals:
                    return vals[0].get("status")
        except Exception:
            return None
        return None

    async def aclose(self) -> None:
        await self._client.aclose()
