"""Low-level transaction building helpers shared by every BUY/SELL path."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.token.associated import get_associated_token_address
from solders.transaction import VersionedTransaction

from . import constants as C


class TxTooLarge(ValueError):
    pass


def ata(owner: Pubkey, mint: Pubkey, token_program: Pubkey = C.TOKEN_PROGRAM) -> Pubkey:
    return get_associated_token_address(owner, mint, token_program)


def create_ata_idempotent(payer: Pubkey, owner: Pubkey, mint: Pubkey,
                          token_program: Pubkey = C.TOKEN_PROGRAM) -> Instruction:
    return Instruction(
        C.ASSOCIATED_TOKEN_PROGRAM,
        bytes([1]),
        [
            AccountMeta(payer, True, True),
            AccountMeta(ata(owner, mint, token_program), False, True),
            AccountMeta(owner, False, False),
            AccountMeta(mint, False, False),
            AccountMeta(C.SYSTEM_PROGRAM, False, False),
            AccountMeta(token_program, False, False),
        ],
    )


def sync_native(account: Pubkey) -> Instruction:
    return Instruction(C.TOKEN_PROGRAM, bytes([17]), [AccountMeta(account, False, True)])


def close_account(account: Pubkey, destination: Pubkey, owner: Pubkey,
                  token_program: Pubkey = C.TOKEN_PROGRAM) -> Instruction:
    return Instruction(
        token_program,
        bytes([9]),
        [AccountMeta(account, False, True), AccountMeta(destination, False, True), AccountMeta(owner, True, False)],
    )


def sol_transfer(src: Pubkey, dst: Pubkey, lamports: int) -> Instruction:
    return transfer(TransferParams(from_pubkey=src, to_pubkey=dst, lamports=lamports))


def compute_budget(cu_limit: int, micro_lamports: int) -> list[Instruction]:
    ixs = [set_compute_unit_limit(cu_limit)]
    if micro_lamports > 0:
        ixs.append(set_compute_unit_price(micro_lamports))
    return ixs


def priority_fee_lamports(cu_limit: int, micro_lamports: int) -> int:
    return (cu_limit * micro_lamports + 999_999) // 1_000_000


def wsol_wrap(user: Pubkey, lamports: int) -> list[Instruction]:
    w = ata(user, C.WSOL_MINT)
    ixs = [create_ata_idempotent(user, user, C.WSOL_MINT)]
    if lamports > 0:
        ixs += [sol_transfer(user, w, lamports), sync_native(w)]
    return ixs


def wsol_unwrap(user: Pubkey) -> Instruction:
    return close_account(ata(user, C.WSOL_MINT), user, user)


@dataclass
class BuiltTx:
    tx: VersionedTransaction
    signature: str
    raw: bytes
    tip_lamports: int
    tip_account: Pubkey | None
    cu_limit: int
    micro_lamports: int

    @property
    def size(self) -> int:
        return len(self.raw)


def compile_and_sign(payer: Keypair, instructions: Sequence[Instruction], blockhash: str,
                     lookup_tables: Sequence[AddressLookupTableAccount] = (),
                     tip_lamports: int = 0, tip_account: Pubkey | None = None,
                     cu_limit: int = 0, micro_lamports: int = 0) -> BuiltTx:
    msg = MessageV0.try_compile(payer.pubkey(), list(instructions), list(lookup_tables),
                                Hash.from_string(blockhash))
    tx = VersionedTransaction(msg, [payer])
    raw = bytes(tx)
    if len(raw) > C.MAX_TX_SIZE:
        raise TxTooLarge(f"transaction is {len(raw)} bytes > {C.MAX_TX_SIZE}")
    return BuiltTx(tx=tx, signature=str(tx.signatures[0]), raw=raw, tip_lamports=tip_lamports,
                   tip_account=tip_account, cu_limit=cu_limit, micro_lamports=micro_lamports)


def decode_lookup_table(key: Pubkey, data: bytes) -> AddressLookupTableAccount:
    """Address lookup table account: 56-byte meta header followed by 32-byte addresses."""
    if len(data) < 56:
        raise ValueError("invalid lookup table account")
    body = data[56:]
    addrs = [Pubkey.from_bytes(body[i:i + 32]) for i in range(0, len(body) - len(body) % 32, 32)]
    return AddressLookupTableAccount(key, addrs)
