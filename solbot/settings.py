"""User-tunable trading parameters (persisted in SQLite, edited from Telegram).

Validation enforces both sane ranges and the operator hard limits from :class:`Config`.
The execution mode defaults to NO_SEND and LIVE can only be enabled when the operator
set ALLOW_LIVE=true in the environment.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .config import Config
from .db import Database

MODE_NO_SEND = "NO_SEND"
MODE_LIVE = "LIVE"


@dataclass(frozen=True)
class Spec:
    key: str
    kind: str  # dec | int | bool | choice
    default: str
    label: str
    lo: Decimal | None = None
    hi: Decimal | None = None
    choices: tuple[str, ...] = ()


SPECS: dict[str, Spec] = {
    s.key: s
    for s in [
        Spec("sol_per_buy", "dec", "0.01", "SOL mỗi lệnh BUY", Decimal("0.0001"), Decimal("1000")),
        Spec("buy_count", "choice", "1", "Số giao dịch BUY", choices=("1", "3", "5")),
        Spec("slippage_pct", "dec", "10", "Slippage BUY %", Decimal("0"), Decimal("100")),
        Spec("sell_slippage_pct", "dec", "15", "Slippage SELL %", Decimal("0"), Decimal("100")),
        Spec("priority_micro_lamports", "int", "100000", "Priority fee (µlamports/CU)", Decimal("0"),
             Decimal("100000000")),
        Spec("cu_limit_pumpswap", "int", "200000", "CU limit PumpSwap", Decimal("50000"), Decimal("1400000")),
        Spec("cu_limit_jupiter", "int", "400000", "CU limit Jupiter", Decimal("100000"), Decimal("1400000")),
        Spec("tip_sol", "dec", "0.0001", "Jito tip (SOL)", Decimal("0.000001"), Decimal("10")),
        Spec("dip_pct", "dec", "20", "DIP %", Decimal("0.01"), Decimal("99.99")),
        Spec("tp_pct", "dec", "0", "TP % (0 = tắt)", Decimal("0"), Decimal("100000")),
        Spec("sl_pct", "dec", "0", "SL % (0 = tắt)", Decimal("0"), Decimal("99.99")),
        Spec("max_sol_per_tx", "dec", "0.5", "Giới hạn SOL/giao dịch", Decimal("0.0001"), Decimal("1000")),
        Spec("max_sol_per_order", "dec", "2.5", "Giới hạn SOL/lệnh", Decimal("0.0001"), Decimal("10000")),
        Spec("max_daily_sol", "dec", "0", "Giới hạn SOL/ngày (0 = không)", Decimal("0"), Decimal("100000")),
        Spec("min_sol_reserve", "dec", "0.02", "SOL giữ lại tối thiểu", Decimal("0"), Decimal("1000")),
        Spec("route", "choice", "auto", "Route", choices=("auto", "pumpswap", "jupiter")),
        Spec("block_buy_on_hold", "bool", "1", "Chặn BUY khi token có lệnh HOLD"),
        Spec("tpsl_max_exit_attempts", "int", "3", "Số lần thử SELL TP/SL", Decimal("1"), Decimal("10")),
        Spec("close_ata_on_full_sell", "bool", "0", "Đóng ATA khi SELL 100%"),
        Spec("mode", "choice", MODE_NO_SEND, "Chế độ", choices=(MODE_NO_SEND, MODE_LIVE)),
        Spec("paused", "bool", "0", "Tạm dừng trigger/auto-sell"),
    ]
}


class SettingsError(ValueError):
    pass


class Settings:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    # -- raw ----------------------------------------------------------------------------------
    def raw(self, key: str) -> str:
        spec = SPECS[key]
        v = self.db.get_setting(key)
        return spec.default if v is None else v

    def dec(self, key: str) -> Decimal:
        return Decimal(self.raw(key))

    def int(self, key: str) -> int:
        return int(Decimal(self.raw(key)))

    def bool(self, key: str) -> bool:
        return self.raw(key) in ("1", "true", "True")

    # -- typed convenience ----------------------------------------------------------------------
    @property
    def mode(self) -> str:
        m = self.raw("mode")
        if m == MODE_LIVE and not self.config.allow_live:
            return MODE_NO_SEND  # operator kill switch wins over the stored value
        return m

    @property
    def is_live(self) -> bool:
        return self.mode == MODE_LIVE

    @property
    def paused(self) -> bool:
        return self.bool("paused")

    def sol_to_lamports(self, sol: Decimal) -> int:
        return int((sol * Decimal(1_000_000_000)).to_integral_value(rounding="ROUND_FLOOR"))

    def tip_lamports(self) -> int:
        return self.sol_to_lamports(self.dec("tip_sol"))

    def slippage_bps(self) -> int:
        return int((self.dec("slippage_pct") * 100).to_integral_value())

    def sell_slippage_bps(self) -> int:
        return int((self.dec("sell_slippage_pct") * 100).to_integral_value())

    # -- writes ----------------------------------------------------------------------------------
    def validate(self, key: str, value: str) -> str:
        if key not in SPECS:
            raise SettingsError(f"Không có tham số {key}")
        spec = SPECS[key]
        value = value.strip().replace(",", ".")
        if spec.kind in ("dec", "int"):
            try:
                d = Decimal(value)
            except InvalidOperation:
                raise SettingsError(f"{spec.label}: giá trị không hợp lệ")
            if not d.is_finite():
                raise SettingsError(f"{spec.label}: giá trị không hợp lệ")
            if spec.kind == "int" and d != d.to_integral_value():
                raise SettingsError(f"{spec.label}: phải là số nguyên")
            if spec.lo is not None and d < spec.lo:
                raise SettingsError(f"{spec.label}: tối thiểu {spec.lo}")
            if spec.hi is not None and d > spec.hi:
                raise SettingsError(f"{spec.label}: tối đa {spec.hi}")
            self._check_hard_limits(key, d)
            return str(d.normalize()) if spec.kind == "dec" else str(int(d))
        if spec.kind == "bool":
            if value.lower() in ("1", "true", "on", "yes", "bật"):
                return "1"
            if value.lower() in ("0", "false", "off", "no", "tắt"):
                return "0"
            raise SettingsError(f"{spec.label}: on/off")
        if spec.kind == "choice":
            if value not in spec.choices:
                raise SettingsError(f"{spec.label}: chọn một trong {', '.join(spec.choices)}")
            return value
        raise SettingsError("unknown kind")

    def _check_hard_limits(self, key: str, d: Decimal) -> None:
        c = self.config
        if key in ("sol_per_buy", "max_sol_per_tx") and d > Decimal(str(c.hard_max_sol_per_tx)):
            raise SettingsError(f"Vượt giới hạn cứng HARD_MAX_SOL_PER_TX={c.hard_max_sol_per_tx}")
        if key == "max_sol_per_order" and d > Decimal(str(c.hard_max_sol_per_order)):
            raise SettingsError(f"Vượt giới hạn cứng HARD_MAX_SOL_PER_ORDER={c.hard_max_sol_per_order}")
        if key == "tip_sol" and d > Decimal(str(c.hard_max_tip_sol)):
            raise SettingsError(f"Vượt giới hạn cứng HARD_MAX_TIP_SOL={c.hard_max_tip_sol}")
        if key == "tip_sol" and d * 1_000_000_000 < 1000:
            raise SettingsError("Jito tip tối thiểu 0.000001 SOL (1000 lamports)")
        if key == "priority_micro_lamports" and d > c.hard_max_priority_micro_lamports:
            raise SettingsError("Vượt giới hạn cứng priority fee")

    def set(self, key: str, value: str) -> str:
        if key == "mode":
            raise SettingsError("Dùng set_mode() để đổi chế độ")
        v = self.validate(key, value)
        self.db.set_setting(key, v)
        self.db.log_event("INFO", "settings", f"{key}={v}")
        return v

    def set_mode(self, mode: str, confirmed_by_user: bool) -> str:
        if mode not in (MODE_NO_SEND, MODE_LIVE):
            raise SettingsError("mode không hợp lệ")
        if mode == MODE_LIVE:
            if not self.config.allow_live:
                raise SettingsError("LIVE bị khóa: operator chưa đặt ALLOW_LIVE=true trên VPS")
            if not confirmed_by_user:
                raise SettingsError("Cần xác nhận rõ ràng của người dùng để bật LIVE")
        self.db.set_setting("mode", mode)
        self.db.log_event("WARN" if mode == MODE_LIVE else "INFO", "mode", f"mode -> {mode}")
        return mode

    def snapshot(self) -> dict[str, str]:
        out = {k: self.raw(k) for k in SPECS}
        out["mode"] = self.mode
        return out
