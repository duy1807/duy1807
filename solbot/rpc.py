"""Async Solana JSON-RPC client with failover, 429 back-off, timeouts and stale-slot detection.

Deliberately has NO sendTransaction method: transactions are only ever sent through Jito
(see :mod:`solbot.jito`). This guarantees no silent fallback to a public RPC.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Sequence

import httpx
from solders.pubkey import Pubkey

log = logging.getLogger("solbot.rpc")


class RpcError(Exception):
    """JSON-RPC level error returned by the node (deterministic; do not fail over blindly)."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class RpcTransportError(Exception):
    """Timeout / connection / HTTP error: safe to retry on another endpoint for reads."""


class RpcRateLimited(RpcTransportError):
    def __init__(self, retry_after: float | None):
        super().__init__(f"HTTP 429 (retry_after={retry_after})")
        self.retry_after = retry_after


class RpcStale(RpcTransportError):
    pass


class RpcMethodUnsupported(RpcError):
    pass


@dataclass
class AccountInfo:
    pubkey: Pubkey
    lamports: int
    owner: Pubkey
    data: bytes
    executable: bool = False


def _host(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]


class HttpJsonRpc:
    def __init__(self, url: str, timeout_ms: int = 2500, headers: dict | None = None,
                 client: httpx.AsyncClient | None = None, name: str | None = None):
        self.url = url
        self.name = name or _host(url)
        self.timeout = timeout_ms / 1000
        self._client = client or httpx.AsyncClient(
            timeout=self.timeout, headers={"content-type": "application/json", **(headers or {})},
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
        self._id = 0
        self.rate_limited_until = 0.0
        self.errors = 0
        self.calls = 0
        self.rate_limited = 0

    async def call(self, method: str, params: list | None = None, timeout: float | None = None) -> Any:
        self._id += 1
        self.calls += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []}
        try:
            resp = await self._client.post(self.url, json=payload, timeout=timeout or self.timeout)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            self.errors += 1
            raise RpcTransportError(f"{self.name} {method}: {type(e).__name__}") from e
        if resp.status_code == 429:
            ra = resp.headers.get("retry-after")
            try:
                ra_s = float(ra) if ra else None
            except ValueError:
                ra_s = None
            self.rate_limited_until = time.monotonic() + min(ra_s or 0.5, 5.0)
            self.rate_limited += 1
            raise RpcRateLimited(ra_s)
        if resp.status_code >= 500:
            self.errors += 1
            raise RpcTransportError(f"{self.name} {method}: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise RpcTransportError(f"{self.name} {method}: HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as e:
            raise RpcTransportError(f"{self.name} {method}: invalid JSON") from e
        if "error" in body and body["error"]:
            err = body["error"]
            code = int(err.get("code", -1))
            msg = str(err.get("message", ""))
            if code == -32601 or "Method not found" in msg:
                raise RpcMethodUnsupported(code, msg, err.get("data"))
            raise RpcError(code, msg, err.get("data"))
        return body.get("result")

    async def aclose(self) -> None:
        await self._client.aclose()


class RpcPool:
    """Read-only failover pool. Endpoint order = preference order."""

    def __init__(self, endpoints: Sequence[HttpJsonRpc], max_slot_lag: int = 30, max_attempts: int = 4):
        if not endpoints:
            raise ValueError("RpcPool needs at least one endpoint")
        self.endpoints = list(endpoints)
        self.max_slot_lag = max_slot_lag
        self.max_attempts = max_attempts
        self.best_slot = 0

    def observe_slot(self, slot: int | None) -> None:
        if slot and slot > self.best_slot:
            self.best_slot = slot

    def _check_ctx(self, ep: HttpJsonRpc, result: Any) -> None:
        if isinstance(result, dict) and isinstance(result.get("context"), dict):
            slot = result["context"].get("slot")
            if isinstance(slot, int):
                if self.best_slot and slot < self.best_slot - self.max_slot_lag:
                    raise RpcStale(f"{ep.name} stale: slot {slot} < best {self.best_slot}")
                self.observe_slot(slot)

    async def call(self, method: str, params: list | None = None, *, stale_check: bool = True,
                   timeout: float | None = None, balance: bool = False) -> Any:
        """``balance=True`` (background polling / blockhash refresh) rotates the starting endpoint so the
        steady load is spread over all providers instead of exhausting the first one's rate limit."""
        last_exc: Exception | None = None
        n = len(self.endpoints)
        start = 0
        if balance and n > 1:
            self._rr = (getattr(self, "_rr", -1) + 1) % n
            start = self._rr
        for attempt in range(self.max_attempts):
            # prefer endpoints that are not currently rate limited
            now = time.monotonic()
            order = sorted(range(n), key=lambda i: (self.endpoints[i].rate_limited_until > now, (i - start) % n))
            ep = self.endpoints[order[attempt % n]]
            try:
                res = await ep.call(method, params, timeout=timeout)
                if stale_check:
                    self._check_ctx(ep, res)
                return res
            except RpcMethodUnsupported:
                raise
            except RpcError:
                raise
            except RpcRateLimited as e:
                last_exc = e
                if n == 1:
                    await asyncio.sleep(min(e.retry_after or (0.15 * 2**attempt), 2.0) + random.random() * 0.05)
            except RpcTransportError as e:
                last_exc = e
                if n == 1:
                    await asyncio.sleep(0.05 * 2**attempt)
        assert last_exc is not None
        raise last_exc

    # -- typed helpers ---------------------------------------------------------------------
    async def get_multiple_accounts(self, keys: Sequence[Pubkey], commitment: str = "processed",
                                    balance: bool = False) -> tuple[int, list[AccountInfo | None]]:
        out: list[AccountInfo | None] = []
        slot = 0
        for i in range(0, len(keys), 100):
            chunk = keys[i:i + 100]
            res = await self.call(
                "getMultipleAccounts",
                [[str(k) for k in chunk], {"encoding": "base64", "commitment": commitment}],
                balance=balance,
            )
            slot = max(slot, int(res["context"]["slot"]))
            for k, v in zip(chunk, res["value"]):
                out.append(parse_account(k, v))
        return slot, out

    async def get_account(self, key: Pubkey, commitment: str = "processed") -> AccountInfo | None:
        _, accs = await self.get_multiple_accounts([key], commitment)
        return accs[0]

    async def get_balance(self, key: Pubkey, commitment: str = "processed") -> int:
        res = await self.call("getBalance", [str(key), {"commitment": commitment}])
        return int(res["value"])

    async def get_latest_blockhash(self, commitment: str = "confirmed", balance: bool = False) -> tuple[str, int, int]:
        res = await self.call("getLatestBlockhash", [{"commitment": commitment}], balance=balance)
        v = res["value"]
        return v["blockhash"], int(v["lastValidBlockHeight"]), int(res["context"]["slot"])

    async def get_block_height(self, commitment: str = "confirmed") -> int:
        return int(await self.call("getBlockHeight", [{"commitment": commitment}], stale_check=False))

    async def get_slot(self, commitment: str = "processed") -> int:
        s = int(await self.call("getSlot", [{"commitment": commitment}], stale_check=False))
        self.observe_slot(s)
        return s

    async def get_signature_statuses(self, sigs: Sequence[str]) -> list[dict | None]:
        res = await self.call(
            "getSignatureStatuses", [list(sigs), {"searchTransactionHistory": True}], stale_check=False
        )
        return list(res["value"])

    async def get_transaction(self, sig: str, commitment: str = "confirmed", max_version: int = 0) -> dict | None:
        return await self.call(
            "getTransaction",
            [sig, {"encoding": "json", "commitment": commitment, "maxSupportedTransactionVersion": max_version}],
            stale_check=False,
        )

    async def get_token_accounts_by_owner_mint(self, owner: Pubkey, mint: Pubkey) -> list[AccountInfo]:
        res = await self.call(
            "getTokenAccountsByOwner",
            [str(owner), {"mint": str(mint)}, {"encoding": "base64", "commitment": "processed"}],
        )
        out = []
        for item in res["value"]:
            acc = parse_account(Pubkey.from_string(item["pubkey"]), item["account"])
            if acc:
                out.append(acc)
        return out

    async def get_program_accounts(self, program: Pubkey, filters: list) -> list[AccountInfo]:
        res = await self.call(
            "getProgramAccounts",
            [str(program), {"encoding": "base64", "filters": filters, "commitment": "confirmed"}],
            stale_check=False, timeout=15,
        )
        items = res if isinstance(res, list) else res.get("value", [])
        out = []
        for item in items:
            acc = parse_account(Pubkey.from_string(item["pubkey"]), item["account"])
            if acc:
                out.append(acc)
        return out

    async def aclose(self) -> None:
        for ep in self.endpoints:
            await ep.aclose()


def parse_account(key: Pubkey, v: dict | None) -> AccountInfo | None:
    if v is None:
        return None
    data = v.get("data")
    if isinstance(data, list):
        raw = base64.b64decode(data[0]) if data and data[1] == "base64" else b""
    elif isinstance(data, str):
        raw = base64.b64decode(data)
    else:
        raw = b""
    return AccountInfo(
        pubkey=key,
        lamports=int(v.get("lamports", 0)),
        owner=Pubkey.from_string(v["owner"]),
        data=raw,
        executable=bool(v.get("executable", False)),
    )


# -- SPL token helpers ---------------------------------------------------------------------------

def token_account_amount(data: bytes) -> int:
    """SPL token account (Token and Token-2022 share the 165-byte base layout)."""
    if len(data) < 72:
        raise ValueError("not a token account")
    return int.from_bytes(data[64:72], "little")


def token_account_mint_owner(data: bytes) -> tuple[Pubkey, Pubkey]:
    return Pubkey.from_bytes(data[0:32]), Pubkey.from_bytes(data[32:64])


def mint_supply_decimals(data: bytes) -> tuple[int, int]:
    if len(data) < 45:
        raise ValueError("not a mint account")
    return int.from_bytes(data[36:44], "little"), data[44]
