"""Jupiter Swap API (v1 quote + swap-instructions) adapter.

We never let Jupiter build the whole transaction: we take its swap instructions and compose our own
transaction (compute budget, Jito tip) so every route goes through the same signing, simulation,
Jito and verification pipeline.

Price-limit enforcement: the on-chain minimum output of a Jupiter v6 route is
``quoted_out_amount * (10000 - slippage_bps) / 10000``. We override the quote's ``outAmount`` with
our required minimum and set ``slippageBps = 0`` so the on-chain minimum equals our minOut, then we
*decode the built instruction* and refuse to send if the encoded minimum is lower than required.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import logging
from dataclasses import dataclass

import httpx
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

from .. import constants as C

log = logging.getLogger("solbot.jupiter")

_ROUTE_DISCS = {
    hashlib.sha256(b"global:route").digest()[:8]: "route",
    hashlib.sha256(b"global:shared_accounts_route").digest()[:8]: "shared_accounts_route",
}


class JupiterError(RuntimeError):
    pass


@dataclass
class JupiterQuote:
    raw: dict
    in_amount: int
    out_amount: int
    other_amount_threshold: int
    price_impact_pct: float
    context_slot: int | None

    @classmethod
    def from_json(cls, j: dict) -> "JupiterQuote":
        try:
            return cls(
                raw=j,
                in_amount=int(j["inAmount"]),
                out_amount=int(j["outAmount"]),
                other_amount_threshold=int(j["otherAmountThreshold"]),
                price_impact_pct=float(j.get("priceImpactPct") or 0),
                context_slot=j.get("contextSlot"),
            )
        except (KeyError, ValueError, TypeError) as e:
            raise JupiterError(f"unexpected quote response: {e}") from e


@dataclass
class JupiterSwapIxs:
    setup: list[Instruction]
    swap: Instruction
    cleanup: list[Instruction]
    other: list[Instruction]
    lookup_tables: list[Pubkey]


def parse_instruction(j: dict) -> Instruction:
    return Instruction(
        Pubkey.from_string(j["programId"]),
        base64.b64decode(j["data"]),
        [AccountMeta(Pubkey.from_string(a["pubkey"]), bool(a["isSigner"]), bool(a["isWritable"]))
         for a in j["accounts"]],
    )


def encoded_min_out(ix: Instruction, expected_in_amount: int) -> int | None:
    """Decode the on-chain minimum output of a Jupiter v6 ExactIn route instruction.

    Returns None when the instruction layout is not recognised (caller must then treat the
    minimum as unverifiable).
    """
    if ix.program_id != C.JUPITER_V6_PROGRAM:
        return None
    data = bytes(ix.data)
    if data[:8] not in _ROUTE_DISCS or len(data) < 8 + 19:
        return None
    tail = data[-19:]
    in_amount = int.from_bytes(tail[0:8], "little")
    quoted_out = int.from_bytes(tail[8:16], "little")
    slippage_bps = int.from_bytes(tail[16:18], "little")
    if in_amount != expected_in_amount or slippage_bps > 10_000:
        return None
    return quoted_out * (10_000 - slippage_bps) // 10_000


def quote_with_min_out(quote: dict, min_out: int) -> dict:
    q = copy.deepcopy(quote)
    q["outAmount"] = str(min_out)
    q["otherAmountThreshold"] = str(min_out)
    q["slippageBps"] = 0
    return q


class JupiterClient:
    def __init__(self, base_url: str, api_key: str | None = None, timeout_ms: int = 2500,
                 client: httpx.AsyncClient | None = None, max_accounts: int = 40):
        self.base = base_url.rstrip("/")
        headers = {"accept": "application/json"}
        if api_key:
            headers["x-api-key"] = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout_ms / 1000, headers=headers)
        self.max_accounts = max_accounts

    async def quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int, slippage_bps: int
                    ) -> JupiterQuote:
        params = {
            "inputMint": str(input_mint),
            "outputMint": str(output_mint),
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "swapMode": "ExactIn",
            "maxAccounts": str(self.max_accounts),
            "restrictIntermediateTokens": "true",
        }
        try:
            r = await self._client.get(f"{self.base}/quote", params=params)
        except httpx.HTTPError as e:
            raise JupiterError(f"quote transport error: {type(e).__name__}") from e
        if r.status_code == 429:
            raise JupiterError("quote rate limited (429)")
        if r.status_code != 200:
            raise JupiterError(f"quote HTTP {r.status_code}: {r.text[:200]}")
        return JupiterQuote.from_json(r.json())

    async def swap_instructions(self, quote_raw: dict, user: Pubkey) -> JupiterSwapIxs:
        body = {
            "quoteResponse": quote_raw,
            "userPublicKey": str(user),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": False,
            "dynamicSlippage": False,
        }
        try:
            r = await self._client.post(f"{self.base}/swap-instructions", json=body)
        except httpx.HTTPError as e:
            raise JupiterError(f"swap-instructions transport error: {type(e).__name__}") from e
        if r.status_code != 200:
            raise JupiterError(f"swap-instructions HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        if j.get("error"):
            raise JupiterError(str(j["error"]))
        try:
            return JupiterSwapIxs(
                setup=[parse_instruction(x) for x in j.get("setupInstructions") or []],
                swap=parse_instruction(j["swapInstruction"]),
                cleanup=[parse_instruction(j["cleanupInstruction"])] if j.get("cleanupInstruction") else [],
                other=[parse_instruction(x) for x in j.get("otherInstructions") or []],
                lookup_tables=[Pubkey.from_string(a) for a in j.get("addressLookupTableAddresses") or []],
            )
        except (KeyError, ValueError) as e:
            raise JupiterError(f"unexpected swap-instructions response: {e}") from e

    async def aclose(self) -> None:
        await self._client.aclose()
