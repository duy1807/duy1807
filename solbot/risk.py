"""Risk Manager: every BUY/SELL passes these checks before anything is signed for sending."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from . import constants as C
from .config import Config
from .db import Database
from .settings import MODE_LIVE, Settings
from .tx_builder import priority_fee_lamports
from .verify import OPEN_SUB, ST_HOLD


class RiskReject(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass
class Funding:
    """Worst-case lamports a submission can take from the wallet."""

    max_swap_in: int = 0
    network_fees: int = 0
    tip: int = 0
    rent: int = 0
    temp_rent: int = 0  # WSOL account rent, returned inside the same tx
    details: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.max_swap_in + self.network_fees + self.tip + self.rent + self.temp_rent


class RiskManager:
    def __init__(self, db: Database, settings: Settings, config: Config):
        self.db = db
        self.settings = settings
        self.config = config

    # -- ledger views -------------------------------------------------------------------------
    def reserved_lamports(self) -> int:
        """Worst-case spend of BUY submissions that are sent but not yet final (HOLD included)."""
        qs = ",".join("?" for _ in OPEN_SUB)
        rows = self.db.query(
            f"SELECT t.max_in_lamports FROM sub_txs t JOIN submissions s ON s.id=t.submission_id "
            f"WHERE s.purpose='BUY' AND s.status IN ({qs}) AND t.status NOT IN ('SUCCESS','FAILED_SAFE','LANDED')",
            tuple(OPEN_SUB))
        return sum(int(r["max_in_lamports"] or 0) for r in rows)

    def daily_spent_lamports(self) -> int:
        start = time.time() - (time.time() % 86400)  # UTC day
        r = self.db.one("SELECT COALESCE(SUM(sol_lamports),0) AS s FROM fills WHERE side='BUY' AND created_at>=?",
                        (start,))
        return int(r["s"]) if r else 0

    def has_hold(self, mint: str) -> bool:
        r = self.db.one("SELECT id FROM submissions WHERE mint=? AND status=? LIMIT 1", (mint, ST_HOLD))
        return r is not None

    def has_inflight(self, mint: str, purpose: str) -> bool:
        qs = ",".join("?" for _ in OPEN_SUB)
        r = self.db.one(f"SELECT id FROM submissions WHERE mint=? AND purpose=? AND status IN ({qs}) LIMIT 1",
                        (mint, purpose, *OPEN_SUB))
        return r is not None

    # -- checks --------------------------------------------------------------------------------
    def check_common(self, exec_mode: str, count: int, tip: int, micro_lamports: int,
                     snapshot_age_ms: float, reasons: list[str]) -> None:
        if exec_mode == MODE_LIVE and not self.settings.is_live:
            reasons.append("Lệnh LIVE nhưng bot đang ở NO_SEND (hoặc ALLOW_LIVE=false)")
        if count not in (1, 3, 5):
            reasons.append("Số giao dịch phải là 1, 3 hoặc 5")
        if tip < C.JITO_MIN_TIP_LAMPORTS:
            reasons.append("Jito tip < 1000 lamports")
        if tip > int(Decimal(str(self.config.hard_max_tip_sol)) * C.LAMPORTS_PER_SOL):
            reasons.append("Jito tip vượt giới hạn cứng")
        if micro_lamports > self.config.hard_max_priority_micro_lamports:
            reasons.append("Priority fee vượt giới hạn cứng")
        if snapshot_age_ms > self.config.snapshot_max_age_ms:
            reasons.append(f"Snapshot cũ {snapshot_age_ms:.0f} ms > {self.config.snapshot_max_age_ms} ms")

    def check_buy(self, *, mint: str, exec_mode: str, count: int, sol_per_tx: int, max_ins: list[int],
                  slippage_bps: int, tip: int, micro_lamports: int, cu_limit: int, snapshot_age_ms: float,
                  wallet_lamports: int, base_ata_exists: bool, token_2022: bool, uva_exists: bool,
                  buy_disabled: bool = False) -> Funding:
        reasons: list[str] = []
        s = self.settings
        self.check_common(exec_mode, count, tip, micro_lamports, snapshot_age_ms, reasons)
        if not 0 <= slippage_bps <= 10_000:
            reasons.append("Slippage phải trong 0–100%")
        if buy_disabled:
            reasons.append("PumpSwap đang tắt BUY (disable_flags)")
        per_tx_cap = s.sol_to_lamports(min(s.dec("max_sol_per_tx"), Decimal(str(self.config.hard_max_sol_per_tx))))
        if sol_per_tx > per_tx_cap:
            reasons.append(f"SOL/giao dịch vượt giới hạn {per_tx_cap / 1e9:g}")
        if any(m > per_tx_cap for m in max_ins):
            reasons.append("maxIn của một giao dịch vượt giới hạn SOL/giao dịch")
        order_cap = s.sol_to_lamports(min(s.dec("max_sol_per_order"),
                                          Decimal(str(self.config.hard_max_sol_per_order))))
        if sum(max_ins) > order_cap:
            reasons.append(f"Tổng lệnh vượt giới hạn {order_cap / 1e9:g} SOL")
        daily = s.dec("max_daily_sol")
        if daily > 0 and self.daily_spent_lamports() + self.reserved_lamports() + sum(max_ins) > s.sol_to_lamports(daily):
            reasons.append("Vượt giới hạn SOL/ngày")
        if s.bool("block_buy_on_hold") and self.has_hold(mint):
            reasons.append("Token có giao dịch UNKNOWN/HOLD chưa đối soát — chặn BUY trùng")
        f = Funding()
        f.max_swap_in = sum(max_ins)
        f.network_fees = count * (C.BASE_FEE_LAMPORTS_PER_SIGNATURE + priority_fee_lamports(cu_limit, micro_lamports))
        f.tip = tip
        if not base_ata_exists:
            f.rent += C.TOKEN_2022_ACCOUNT_RENT_MAX_LAMPORTS if token_2022 else C.TOKEN_ACCOUNT_RENT_LAMPORTS
        if not uva_exists:
            f.rent += 3_000_000  # user_volume_accumulator (upper bound)
        f.temp_rent = C.TOKEN_ACCOUNT_RENT_LAMPORTS
        need = f.total + s.sol_to_lamports(s.dec("min_sol_reserve")) + self.reserved_lamports()
        f.details = {"need": need, "wallet": wallet_lamports}
        if wallet_lamports < need:
            reasons.append(f"Số dư không đủ: cần {need / 1e9:.6f} SOL (gồm phí, tip, rent, dự trữ), có "
                           f"{wallet_lamports / 1e9:.6f}")
        if reasons:
            raise RiskReject(reasons)
        return f

    def check_sell(self, *, exec_mode: str, tip: int, micro_lamports: int, cu_limit: int, snapshot_age_ms: float,
                   wallet_lamports: int, amount_raw: int, sell_disabled: bool = False) -> Funding:
        reasons: list[str] = []
        self.check_common(exec_mode, 1, tip, micro_lamports, snapshot_age_ms, reasons)
        if amount_raw <= 0:
            reasons.append("Không có token để bán")
        if sell_disabled:
            reasons.append("PumpSwap đang tắt SELL (disable_flags)")
        f = Funding(network_fees=C.BASE_FEE_LAMPORTS_PER_SIGNATURE + priority_fee_lamports(cu_limit, micro_lamports),
                    tip=tip, temp_rent=C.TOKEN_ACCOUNT_RENT_LAMPORTS)
        if wallet_lamports < f.total:
            reasons.append(f"Không đủ SOL trả phí SELL: cần {f.total / 1e9:.6f}")
        if reasons:
            raise RiskReject(reasons)
        return f
