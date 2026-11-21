"""On-chain constants: program ids, PDAs, discriminators, well-known accounts.

Sources: pump-fun/pump-public-docs (idl/pump_amm.json, docs/instructions/PUMP_SWAP_TRADE_V2.md,
docs/FEE_RECIPIENTS.md) and @pump-fun/pump-swap-sdk 2.1.0 (src/sdk/pda.ts).
Instruction discriminators are copied from the IDL and re-checked against
sha256("global:<name>")[:8] in tests/test_pumpswap.py.
"""
from __future__ import annotations

from solders.pubkey import Pubkey

LAMPORTS_PER_SOL = 1_000_000_000

# --- Programs -------------------------------------------------------------------------------
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
COMPUTE_BUDGET_PROGRAM = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
METADATA_PROGRAM = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")

PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_AMM_PROGRAM = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
PUMP_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

JUPITER_V6_PROGRAM = Pubkey.from_string("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")

# --- Mints ----------------------------------------------------------------------------------
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
NATIVE_MINT_2022 = Pubkey.from_string("9pan9bMn5HatX4EJdBwg9VgCa7Uz5HL8N1m5D3NdXejP")

# --- PumpSwap PDAs --------------------------------------------------------------------------
PUMP_AMM_GLOBAL_CONFIG = Pubkey.find_program_address([b"global_config"], PUMP_AMM_PROGRAM)[0]
PUMP_AMM_EVENT_AUTHORITY = Pubkey.find_program_address([b"__event_authority"], PUMP_AMM_PROGRAM)[0]
PUMP_AMM_FEE_CONFIG = Pubkey.find_program_address(
    [b"fee_config", bytes(PUMP_AMM_PROGRAM)], PUMP_FEE_PROGRAM
)[0]
CANONICAL_POOL_INDEX = 0
# pump-amm TOTAL_TOKEN_SUPPLY, market-cap basis for mayhem-mode pools
PUMP_AMM_TOTAL_TOKEN_SUPPLY = 1_000_000_000_000_000


def pump_pool_authority(mint: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([b"pool-authority", bytes(mint)], PUMP_PROGRAM)[0]


def pump_pool_pda(index: int, creator: Pubkey, base_mint: Pubkey, quote_mint: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [b"pool", index.to_bytes(2, "little"), bytes(creator), bytes(base_mint), bytes(quote_mint)],
        PUMP_AMM_PROGRAM,
    )[0]


def canonical_pump_pool(mint: Pubkey, quote_mint: Pubkey = WSOL_MINT) -> Pubkey:
    return pump_pool_pda(CANONICAL_POOL_INDEX, pump_pool_authority(mint), mint, quote_mint)


def user_volume_accumulator(user: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([b"user_volume_accumulator", bytes(user)], PUMP_AMM_PROGRAM)[0]


def metadata_pda(mint: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [b"metadata", bytes(METADATA_PROGRAM), bytes(mint)], METADATA_PROGRAM
    )[0]


# --- PumpSwap discriminators (from IDL) ------------------------------------------------------
IX_BUY_V2 = bytes([184, 23, 238, 97, 103, 197, 211, 61])
IX_SELL_V2 = bytes([93, 246, 130, 60, 231, 233, 64, 178])
IX_BUY_EXACT_QUOTE_IN_V2_NAME = "buy_exact_quote_in_v2"

# Buyback fee recipients (docs/FEE_RECIPIENTS.md). GlobalConfig.buyback_fee_recipients is
# preferred at runtime; this list is the fallback when GlobalConfig cannot be decoded.
BUYBACK_FEE_RECIPIENTS = [
    Pubkey.from_string(s)
    for s in (
        "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD",
        "9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7",
        "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL",
        "3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR",
        "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6",
        "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL",
        "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD",
        "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW",
    )
]

# --- Jito -----------------------------------------------------------------------------------
# Fallback list; the bot refreshes it from getTipAccounts at startup.
JITO_TIP_ACCOUNTS = [
    Pubkey.from_string(s)
    for s in (
        "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
        "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
        "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
        "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
        "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
        "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
        "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
        "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
    )
]
JITO_MIN_TIP_LAMPORTS = 1_000
JITO_MAX_BUNDLE_TXS = 5

# --- Sizes / rent ---------------------------------------------------------------------------
TOKEN_ACCOUNT_RENT_LAMPORTS = 2_039_280  # 165-byte SPL token account
TOKEN_2022_ACCOUNT_RENT_MAX_LAMPORTS = 2_500_000  # with extensions (upper estimate)
BASE_FEE_LAMPORTS_PER_SIGNATURE = 5_000
MAX_TX_SIZE = 1232
