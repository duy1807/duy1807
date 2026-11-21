"""Telegram controller (raw Bot API over httpx long polling).

* Only user ids in TELEGRAM_ALLOWED_USER_IDS are served.
* Every trade requires an explicit confirmation button; confirmations carry a one-time token.
* Telegram I/O never sits on the trading path: orders run in background tasks and notifications
  go through an outbound queue.
* LIVE can only be enabled when the operator set ALLOW_LIVE=true and the user types the
  confirmation phrase; the wallet key is never displayed or accepted through Telegram.
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import httpx
from solders.pubkey import Pubkey

from .engine import fmt_amount, sol_str
from .logging_setup import redact
from .orders import OrderError, OrderParams
from .settings import MODE_LIVE, MODE_NO_SEND, SPECS, SettingsError

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("solbot.telegram")

KIND_CODE = {"N": "NOW", "D": "DIP", "L": "LIMIT"}
KIND_LABEL = {"NOW": "BUY NGAY", "DIP": "FIXED DIP", "LIMIT": "LIMIT BUY"}
DRAFT_FIELDS = {
    "sol": ("SOL mỗi lệnh BUY", "sol_per_buy"),
    "slip": ("Slippage BUY %", "slippage_pct"),
    "tp": ("TP % (0 = tắt)", "tp_pct"),
    "sl": ("SL % (0 = tắt)", "sl_pct"),
    "dip": ("DIP %", "dip_pct"),
    "lim": ("Giá LIMIT (SOL/token)", None),
}


def esc(x: Any) -> str:
    return html.escape(redact(str(x)), quote=False)


def fmt_price(p: Decimal | str | None) -> str:
    if p is None or p == "":
        return "—"
    p = Decimal(p)
    if p == 0:
        return "0"
    s = f"{p:.18f}".rstrip("0").rstrip(".")
    if "." in s:
        intpart, frac = s.split(".")
        lead = len(frac) - len(frac.lstrip("0"))
        frac = frac[: lead + 6]
        s = intpart + "." + frac
    return s


def short(k: str, n: int = 4) -> str:
    return f"{k[:n]}…{k[-n:]}"


def is_pubkey(text: str) -> bool:
    if not 32 <= len(text) <= 44:
        return False
    try:
        Pubkey.from_string(text)
        return True
    except Exception:
        return False


@dataclass
class ChatSession:
    mint: str | None = None
    draft: dict = field(default_factory=dict)
    awaiting: tuple | None = None  # (kind, arg)
    confirm: dict | None = None  # {"token":..., "action":..., ...}
    card_msg_id: int | None = None


class TelegramBot:
    def __init__(self, app: "App", transport: httpx.AsyncBaseTransport | None = None):
        self.app = app
        cfg = app.config
        self.base = f"{cfg.telegram_api_base}/bot{cfg.telegram_token}"
        self.allowed = set(cfg.telegram_allowed_user_ids)
        self.http = httpx.AsyncClient(transport=transport, timeout=35)
        self.sessions: dict[int, ChatSession] = {}
        self.outbox: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._offset = 0
        self.default_chat = next(iter(self.allowed), None)
        from .logging_setup import register_secret

        register_secret(cfg.telegram_token)

    # ------------------------------------------------------------------- transport --
    async def api(self, method: str, **params) -> Any:
        for attempt in range(5):
            try:
                r = await self.http.post(f"{self.base}/{method}", json=params)
            except httpx.HTTPError as e:
                log.warning("telegram %s failed: %s", method, type(e).__name__)
                await asyncio.sleep(1 + attempt)
                continue
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 429:
                await asyncio.sleep(float((body.get("parameters") or {}).get("retry_after", 1)))
                continue
            if not body.get("ok"):
                desc = body.get("description", "")
                if "message is not modified" in desc:
                    return None
                log.warning("telegram %s error: %s", method, redact(desc))
                return None
            return body.get("result")
        return None

    def notify(self, chat_id: int | None, text: str) -> None:
        """Non-blocking: queue a message (used by the engine)."""
        chat = chat_id or self.default_chat
        if chat is not None:
            self.outbox.put_nowait((chat, text, None))

    async def send(self, chat_id: int, text: str, kb: list | None = None) -> dict | None:
        return await self.api("sendMessage", chat_id=chat_id, text=text[:4000], parse_mode="HTML",
                              disable_web_page_preview=True, reply_markup=self._markup(kb))

    async def edit(self, chat_id: int, msg_id: int, text: str, kb: list | None = None) -> None:
        await self.api("editMessageText", chat_id=chat_id, message_id=msg_id, text=text[:4000], parse_mode="HTML",
                       disable_web_page_preview=True, reply_markup=self._markup(kb))

    @staticmethod
    def _markup(kb):
        if not kb:
            return None
        return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in kb]}

    async def _sender(self) -> None:
        while True:
            chat, text, kb = await self.outbox.get()
            try:
                await self.send(chat, esc(text) if kb is None else text, kb)
            except Exception:
                log.exception("telegram send failed")

    async def _poller(self) -> None:
        while True:
            try:
                ups = await self.api("getUpdates", offset=self._offset, timeout=25,
                                     allowed_updates=["message", "callback_query"])
            except Exception:
                ups = None
            if not ups:
                await asyncio.sleep(0.2)
                continue
            for u in ups:
                self._offset = max(self._offset, u["update_id"] + 1)
                asyncio.create_task(self._safe_handle(u))

    async def _safe_handle(self, u: dict) -> None:
        try:
            await self.handle_update(u)
        except Exception as e:
            log.exception("telegram handler error")
            chat = (u.get("message") or (u.get("callback_query") or {}).get("message") or {}).get("chat", {}).get("id")
            if chat:
                await self.send(chat, f"⚠️ Lỗi: {esc(e)}")

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._poller(), name="tg-poll"),
                       asyncio.create_task(self._sender(), name="tg-send")]
        for chat in self.allowed:
            self.notify(chat, f"🤖 Bot khởi động — chế độ {self.app.settings.mode}. "
                              f"Khôi phục: {self.app.recovery_report}")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.http.aclose()

    # --------------------------------------------------------------------- routing --
    def session(self, chat: int) -> ChatSession:
        return self.sessions.setdefault(chat, ChatSession())

    async def handle_update(self, u: dict) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            uid = cq["from"]["id"]
            if uid not in self.allowed:
                await self.api("answerCallbackQuery", callback_query_id=cq["id"], text="Không có quyền")
                return
            await self.api("answerCallbackQuery", callback_query_id=cq["id"])
            msg = cq.get("message") or {}
            await self.on_callback(msg["chat"]["id"], msg.get("message_id"), cq.get("data") or "")
            return
        msg = u.get("message")
        if not msg or "text" not in msg:
            return
        uid = msg["from"]["id"]
        if uid not in self.allowed:
            log.warning("unauthorized telegram user id %s", uid)
            return
        await self.on_text(msg["chat"]["id"], msg["text"].strip())

    async def on_text(self, chat: int, text: str) -> None:
        s = self.session(chat)
        if text.startswith("/"):
            cmd = text.split()[0].split("@")[0].lower()
            s.awaiting = None
            if cmd in ("/start", "/menu"):
                return await self.show_menu(chat)
            if cmd == "/orders":
                return await self.show_orders(chat)
            if cmd == "/positions":
                return await self.show_positions(chat)
            if cmd == "/history":
                return await self.show_history(chat)
            if cmd == "/hold":
                return await self.show_holds(chat)
            if cmd == "/settings":
                return await self.show_settings(chat)
            if cmd == "/stats":
                return await self.show_stats(chat)
            return await self.send(chat, "Lệnh: /menu /orders /positions /history /hold /settings /stats\n"
                                         "Hoặc dán địa chỉ mint token.")
        if s.awaiting:
            return await self.on_input(chat, s, text)
        if is_pubkey(text):
            s.mint = text
            s.draft = {}
            s.confirm = None
            return await self.show_token(chat, None)
        await self.send(chat, "Dán địa chỉ mint token hoặc dùng /menu.")

    # ------------------------------------------------------------------- screens --
    async def show_menu(self, chat: int, msg_id: int | None = None) -> None:
        a = self.app
        try:
            bal = await a.rpc.get_balance(a.wallet.pubkey)
        except Exception:
            bal = None
        armed = a.db.one("SELECT COUNT(*) c FROM orders WHERE status='ARMED'")["c"]
        npos = len(a.positions.list_open())
        holds = a.db.one("SELECT COUNT(*) c FROM submissions WHERE status='UNKNOWN_HOLD'")["c"]
        mode = a.settings.mode
        text = (f"🤖 <b>Solana Bot</b>\n"
                f"Ví: <code>{a.wallet.pubkey}</code>\n"
                f"Số dư: {sol_str(bal, 4) if bal is not None else '—'} SOL\n"
                f"Chế độ: <b>{'🟢 LIVE' if mode == MODE_LIVE else '🧪 NO_SEND'}</b>"
                f"{' (ALLOW_LIVE=false)' if not a.config.allow_live else ''}\n"
                f"Trigger/Auto-sell: {'⏸ TẠM DỪNG' if a.settings.paused else '▶️ chạy'}\n"
                f"Lệnh chờ: {armed} | Vị thế mở: {npos} | HOLD: {holds}\n\n"
                f"Dán địa chỉ mint để mở giao diện token.")
        kb = [[("📋 Lệnh chờ", "ol"), ("📈 Vị thế", "pl")],
              [("📜 Lịch sử", "h"), (f"⚠️ HOLD ({holds})", "hd")],
              [("⚙️ Cài đặt", "st"), ("📊 Hiệu năng", "sx")],
              [(f"🔁 Chế độ: {mode}", "md"), ("▶️ Tiếp tục" if a.settings.paused else "⏸ Tạm dừng", "pz")]]
        await self._render(chat, msg_id, text, kb)

    async def _render(self, chat: int, msg_id: int | None, text: str, kb) -> None:
        if msg_id:
            await self.edit(chat, msg_id, text, kb)
        else:
            await self.send(chat, text, kb)

    def params_for(self, s: ChatSession) -> OrderParams:
        p = self.app.orders.default_params()
        d = s.draft
        if "sol" in d:
            p.sol_per_buy = d["sol"]
        if "slip" in d:
            p.slippage_pct = d["slip"]
        if "tp" in d:
            p.tp_pct = d["tp"]
        if "sl" in d:
            p.sl_pct = d["sl"]
        if "dip" in d:
            p.dip_pct = d["dip"]
        if "lim" in d:
            p.limit_price = d["lim"]
        return p

    async def show_token(self, chat: int, msg_id: int | None) -> None:
        a = self.app
        s = self.session(chat)
        if not s.mint:
            return await self.send(chat, "Chưa chọn token.")
        mint = Pubkey.from_string(s.mint)
        try:
            m = await a.market.resolve(mint, a.settings.raw("route"))
            snap = await a.market.fresh_snapshot(mint)
        except Exception as e:
            return await self._render(chat, msg_id, f"❌ Không đọc được token: {esc(e)}", [[("🏠 Menu", "m")]])
        info = m.info
        try:
            accts = await a.engine._user_accounts(mint, info.token_program)
        except Exception:
            accts = {"wallet_lamports": None, "base_amount": 0}
        pos = a.positions.open_position(s.mint)
        lines = [f"🪙 <b>{esc(info.name or 'Token')}</b> ({esc(info.symbol or '?')})",
                 f"Mint: <code>{esc(s.mint)}</code>"]
        if m.pool:
            lines.append(f"Pool: PumpSwap <code>{short(str(m.pool.address))}</code>"
                         f"{' (canonical)' if m.pool.is_pump_pool else ''}")
        else:
            lines.append("Route: Jupiter")
        lines.append(f"Giá: <b>{fmt_price(snap.price_sol)}</b> SOL/token (snapshot {snap.age_ms():.0f} ms, "
                     f"slot {snap.slot})")
        if snap.ps_state:
            st = snap.ps_state
            lines.append(f"Market Cap: {st.market_cap_sol():,.2f} SOL | Thanh khoản: {st.liquidity_sol():,.2f} SOL")
            try:
                lines.append(f"Phí pool: {st.fees().total_bps / 100:.2f}%")
            except Exception:
                pass
        lines.append(f"Số dư SOL: {sol_str(accts['wallet_lamports'], 4)} | Token: "
                     f"{fmt_amount(accts['base_amount'], info.decimals)}")
        if pos:
            tok = int(pos["token_raw"])
            value = None
            if snap.ps_state and tok > 0:
                try:
                    value = snap.ps_state.quote_sell(tok).user_out
                except Exception:
                    value = None
            pnl = a.positions.pnl_pct(pos["cost_lamports"], value) if value is not None else None
            lines.append(f"Giá vốn: {sol_str(pos['cost_lamports'], 4)} SOL (TB {fmt_price(a.positions.avg_entry_price(pos))})"
                         f" | Giá trị: {sol_str(value, 4)} | PnL: {pnl if pnl is not None else '—'}%")
            lines.append(f"TP/SL: {pos['tp_pct'] or '—'}% / {pos['sl_pct'] or '—'}% "
                         f"({'bật' if pos['tpsl_enabled'] else 'tắt'})")
        p = self.params_for(s)
        lines += ["", f"<b>Tham số:</b> {p.sol_per_buy} SOL/lệnh | Slip {p.slippage_pct}% | TP {p.tp_pct}% | "
                      f"SL {p.sl_pct}% | DIP {p.dip_pct}% | LIMIT {fmt_price(p.limit_price)}",
                  f"Chế độ: <b>{a.settings.mode}</b>"]
        kb = [
            [("BUY NGAY ×1", "b:N:1"), ("×3", "b:N:3"), ("×5", "b:N:5")],
            [("DIP BUY ×1", "b:D:1"), ("×3", "b:D:3"), ("×5", "b:D:5")],
            [("LIMIT BUY ×1", "b:L:1"), ("×3", "b:L:3"), ("×5", "b:L:5")],
            [("SELL 25%", "s:25"), ("50%", "s:50"), ("75%", "s:75"), ("100%", "s:100"), ("%…", "sc")],
            [(f"SOL {p.sol_per_buy}", "p:sol"), (f"Slip {p.slippage_pct}%", "p:slip"), (f"TP {p.tp_pct}%", "p:tp"),
             (f"SL {p.sl_pct}%", "p:sl")],
            [(f"DIP {p.dip_pct}%", "p:dip"), (f"LIMIT {fmt_price(p.limit_price)}", "p:lim"), ("🔄", "tk"),
             ("🏠", "m")],
        ]
        await self._render(chat, msg_id, "\n".join(lines), kb)

    # ------------------------------------------------------------------ callbacks --
    async def on_callback(self, chat: int, msg_id: int | None, data: str) -> None:
        s = self.session(chat)
        parts = data.split(":")
        head = parts[0]
        if head == "m":
            return await self.show_menu(chat, msg_id)
        if head == "tk":
            return await self.show_token(chat, msg_id)
        if head == "b":
            return await self.prompt_buy(chat, msg_id, KIND_CODE[parts[1]], int(parts[2]))
        if head == "s":
            return await self.prompt_sell(chat, msg_id, s.mint, Decimal(parts[1]))
        if head == "sc":
            s.awaiting = ("sellpct", s.mint)
            return await self.send(chat, "Nhập tỷ lệ % muốn bán (0–100]:")
        if head == "p":
            f = parts[1]
            s.awaiting = ("draft", f)
            return await self.send(chat, f"Nhập {DRAFT_FIELDS[f][0]}:")
        if head == "cf":
            return await self.on_confirm(chat, msg_id, parts[1])
        if head == "cx":
            s.confirm = None
            return await self._render(chat, msg_id, "Đã hủy.", [[("🔙 Token", "tk"), ("🏠 Menu", "m")]])
        if head == "ol":
            return await self.show_orders(chat, msg_id)
        if head == "oc":
            ok = self.app.orders.cancel(int(parts[1]))
            await self.send(chat, f"{'✅ Đã hủy' if ok else '❌ Không hủy được'} lệnh #{parts[1]}")
            return await self.show_orders(chat, msg_id)
        if head == "od":
            return await self.show_order_detail(chat, int(parts[1]))
        if head == "pl":
            return await self.show_positions(chat, msg_id)
        if head == "ps":
            pos = self.app.positions.get(int(parts[1]))
            if pos:
                return await self.prompt_sell(chat, msg_id, pos["mint"], Decimal(parts[2]))
        if head == "pt":
            pos = self.app.positions.get(int(parts[1]))
            if pos:
                self.app.positions.set_tpsl(pos["id"], enabled=not pos["tpsl_enabled"])
            return await self.show_positions(chat, msg_id)
        if head in ("ptp", "psl"):
            s.awaiting = (head, int(parts[1]))
            return await self.send(chat, f"Nhập {'TP' if head == 'ptp' else 'SL'} % mới cho vị thế #{parts[1]} (0 = tắt):")
        if head == "h":
            return await self.show_history(chat, msg_id)
        if head == "hd":
            return await self.show_holds(chat, msg_id)
        if head == "hr":
            sid = int(parts[1])
            asyncio.create_task(self.app.verifier.run(sid, deadline_s=20))
            return await self.send(chat, f"🔄 Đang đối soát lại submission #{sid} với 2 RPC…")
        if head == "st":
            return await self.show_settings(chat, msg_id)
        if head == "se":
            s.awaiting = ("setting", parts[1])
            spec = SPECS[parts[1]]
            extra = f" ({'/'.join(spec.choices)})" if spec.choices else ""
            return await self.send(chat, f"Nhập giá trị mới cho {spec.label}{extra}:")
        if head == "md":
            return await self.toggle_mode(chat, msg_id)
        if head == "pz":
            self.app.settings.set("paused", "0" if self.app.settings.paused else "1")
            return await self.show_menu(chat, msg_id)
        if head == "sx":
            return await self.show_stats(chat, msg_id)

    async def on_input(self, chat: int, s: ChatSession, text: str) -> None:
        kind, arg = s.awaiting
        s.awaiting = None
        try:
            if kind == "draft":
                if arg == "lim":
                    v = Decimal(text.replace(",", "."))
                    if v <= 0:
                        raise SettingsError("Giá LIMIT phải > 0")
                    s.draft["lim"] = v
                else:
                    key = DRAFT_FIELDS[arg][1]
                    s.draft[arg] = Decimal(self.app.settings.validate(key, text))
                return await self.show_token(chat, None)
            if kind == "sellpct":
                return await self.prompt_sell(chat, None, arg, Decimal(text.replace(",", ".").rstrip("%")))
            if kind in ("ptp", "psl"):
                v = Decimal(self.app.settings.validate("tp_pct" if kind == "ptp" else "sl_pct", text))
                if kind == "ptp":
                    self.app.positions.set_tpsl(arg, tp_pct=v, enabled=True)
                else:
                    self.app.positions.set_tpsl(arg, sl_pct=v, enabled=True)
                return await self.show_positions(chat)
            if kind == "setting":
                v = self.app.settings.set(arg, text)
                await self.send(chat, f"✅ {esc(SPECS[arg].label)} = {esc(v)}")
                return await self.show_settings(chat)
            if kind == "live":
                return await self.confirm_live(chat, text)
        except (SettingsError, InvalidOperation, ValueError) as e:
            await self.send(chat, f"❌ {esc(e)}")

    # ------------------------------------------------------------------ BUY / SELL --
    async def prompt_buy(self, chat: int, msg_id: int | None, kind: str, count: int) -> None:
        s = self.session(chat)
        a = self.app
        if not s.mint:
            return await self.send(chat, "Chưa chọn token.")
        p = self.params_for(s)
        p.count = count
        if kind == "LIMIT" and p.limit_price is None:
            s.awaiting = ("draft", "lim")
            return await self.send(chat, "Nhập giá LIMIT (SOL/token) trước khi ARM:")
        snap = a.market.current(Pubkey.from_string(s.mint))
        cur = snap.price_sol if snap else None
        total = p.sol_per_buy * count
        lines = [f"<b>{KIND_LABEL[kind]} ×{count}</b> — xác nhận",
                 f"Token: <code>{esc(s.mint)}</code>",
                 f"SOL mỗi lệnh: {p.sol_per_buy} × {count} = <b>{total} SOL</b> (chưa gồm phí)",
                 f"Slippage: {p.slippage_pct}% (tối đa {p.sol_per_buy * (1 + p.slippage_pct / 100):.6f} SOL/lệnh)",
                 f"TP/SL: {p.tp_pct}% / {p.sl_pct}%",
                 f"Priority fee: {a.settings.int('priority_micro_lamports')} µlamports/CU | "
                 f"Jito tip: {a.settings.dec('tip_sol')} SOL",
                 f"Giá hiện tại: {fmt_price(cur)}"]
        if kind == "DIP":
            if cur:
                lines.append(f"DIP {p.dip_pct}% → giá kích hoạt ≈ {fmt_price(cur * (1 - p.dip_pct / 100))} "
                             f"(chốt giá tham chiếu khi ARM)")
        if kind == "LIMIT":
            lines.append(f"Giá LIMIT: <b>{fmt_price(p.limit_price)}</b>")
            if cur and p.limit_price >= cur:
                lines.append("⚠️ LIMIT ≥ giá hiện tại: lệnh có thể kích hoạt ngay.")
        if kind != "NOW":
            lines.append("Giá thực hiện của MỖI giao dịch (kể cả phí pool) sẽ không vượt giá kích hoạt.")
        lines.append(f"Chế độ: <b>{a.settings.mode}</b>")
        token = secrets.token_hex(4)
        s.confirm = {"token": token, "action": "buy", "kind": kind, "mint": s.mint, "params": p, "ts": time.time()}
        kb = [[(f"✅ {'BUY' if kind == 'NOW' else 'ARM'} ×{count}", f"cf:{token}"), ("❌ Hủy", "cx")]]
        await self._render(chat, msg_id, "\n".join(lines), kb)

    async def prompt_sell(self, chat: int, msg_id: int | None, mint: str | None, pct: Decimal) -> None:
        if not mint:
            return await self.send(chat, "Chưa chọn token.")
        if not (Decimal(0) < pct <= 100):
            return await self.send(chat, "❌ Tỷ lệ phải trong (0, 100].")
        s = self.session(chat)
        token = secrets.token_hex(4)
        s.confirm = {"token": token, "action": "sell", "mint": mint, "pct": pct, "ts": time.time()}
        text = (f"<b>SELL {pct}%</b> token <code>{esc(mint)}</code>\n"
                f"Slippage SELL: {self.app.settings.dec('sell_slippage_pct')}% | Chế độ: {self.app.settings.mode}\n"
                f"Số lượng tính theo số dư token thực tế trên ví lúc gửi.")
        await self._render(chat, msg_id, text, [[(f"✅ SELL {pct}%", f"cf:{token}"), ("❌ Hủy", "cx")]])

    async def on_confirm(self, chat: int, msg_id: int | None, token: str) -> None:
        s = self.session(chat)
        c = s.confirm
        if not c or c["token"] != token or time.time() - c["ts"] > 120:
            return await self.send(chat, "⌛ Xác nhận đã hết hạn hoặc không hợp lệ. Vui lòng thao tác lại.")
        s.confirm = None
        if c["action"] == "buy":
            try:
                o = await self.app.orders.create(c["kind"], c["mint"], c["params"], chat)
            except (OrderError, SettingsError) as e:
                return await self.send(chat, f"❌ {esc(e)}")
            except Exception as e:
                return await self.send(chat, f"❌ Không tạo được lệnh: {esc(e)}")
            if o["kind"] == "NOW":
                return await self._render(chat, msg_id, f"⏳ Lệnh #{o['id']} BUY NGAY ×{o['count']} đang xử lý "
                                                        f"({o['exec_mode']})…", [[("📜 Chi tiết", f"od:{o['id']}")]])
            return await self._render(
                chat, msg_id,
                f"🎯 Đã ARM lệnh #{o['id']} {o['kind']} ×{o['count']}\n"
                f"Giá tham chiếu: {fmt_price(o['reference_price'])}\nGiá kích hoạt (trần): "
                f"<b>{fmt_price(o['trigger_price'])}</b>\nChế độ: {o['exec_mode']}",
                [[("📋 Lệnh chờ", "ol"), ("🔙 Token", "tk")]])
        if c["action"] == "sell":
            res_task = asyncio.create_task(self.app.engine.execute_sell(c["mint"], c["pct"], "MANUAL", chat))
            await self._render(chat, msg_id, f"⏳ SELL {c['pct']}% đang xử lý…", [[("🔙 Token", "tk")]])
            res = await res_task
            if res.status == "DRY_RUN":
                await self.send(chat, f"🧪 NO_SEND: SELL {c['pct']}% đã chuẩn bị + mô phỏng, KHÔNG gửi.\n{esc(res.message)}")

    # --------------------------------------------------------------- order lists --
    async def show_orders(self, chat: int, msg_id: int | None = None) -> None:
        a = self.app
        rows = a.db.query("SELECT * FROM orders WHERE status IN ('ARMED','TRIGGERED','SENT','HOLD') ORDER BY id DESC "
                          "LIMIT 20")
        if not rows:
            return await self._render(chat, msg_id, "Không có lệnh đang chờ.", [[("🏠 Menu", "m")]])
        lines = ["<b>Lệnh DIP / LIMIT đang chờ</b>"]
        kb = []
        for o in rows:
            snap = a.market.current(Pubkey.from_string(o["mint"]))
            cur = snap.price_sol if snap else None
            dist = ""
            if cur and o["trigger_price"]:
                dist = f" ({(cur / Decimal(o['trigger_price']) - 1) * 100:+.2f}% so với trigger)"
            lines.append(
                f"\n#{o['id']} <b>{o['kind']} ×{o['count']}</b> {short(o['mint'])} — {o['status']} ({o['exec_mode']})\n"
                f"  Tham chiếu: {fmt_price(o['reference_price'])} | Kích hoạt: {fmt_price(o['trigger_price'])}\n"
                f"  Hiện tại: {fmt_price(cur)}{dist}\n"
                f"  {Decimal(o['sol_per_buy_lamports']) / 10**9} SOL/lệnh | TP {o['tp_pct'] or '—'}% SL {o['sl_pct'] or '—'}%"
                + (f"\n  Ghi chú: {esc(o['wait_note'])}" if o["wait_note"] else ""))
            row = [(f"🔎 #{o['id']}", f"od:{o['id']}")]
            if o["status"] == "ARMED":
                row.append((f"❌ Hủy #{o['id']}", f"oc:{o['id']}"))
            kb.append(row)
        kb.append([("🔄", "ol"), ("🏠 Menu", "m")])
        await self._render(chat, msg_id, "\n".join(lines), kb)

    async def show_order_detail(self, chat: int, oid: int) -> None:
        a = self.app
        o = a.db.get("orders", oid)
        if not o:
            return await self.send(chat, "Không tìm thấy lệnh.")
        lines = [f"<b>Lệnh #{oid}</b> {o['kind']} ×{o['count']} — {o['status']} ({o['exec_mode']})",
                 f"Token: <code>{o['mint']}</code> | Route: {o['route']}",
                 f"{Decimal(o['sol_per_buy_lamports']) / 10**9} SOL/lệnh | Slip {o['slippage_bps'] / 100}% | "
                 f"Tip {sol_str(o['tip_lamports'])}",
                 f"Tham chiếu: {fmt_price(o['reference_price'])} | Kích hoạt/trần: {fmt_price(o['trigger_price'])}"]
        if o["status_reason"]:
            lines.append(f"Lý do: {esc(o['status_reason'])}")
        if o["submission_id"]:
            sub = a.db.get("submissions", o["submission_id"])
            lines += [f"\n<b>Submission #{sub['id']}</b> {sub['status']} | sim: {esc(sub['sim_detail'] or '')}",
                      f"Bundle: <code>{sub['bundle_id'] or '—'}</code> | blockhash {short(sub['blockhash'] or '')} "
                      f"| lastValid {sub['last_valid_block_height']}",
                      f"Snapshot slot {sub['snapshot_slot']} ({(sub['snapshot_age_ms'] or 0):.0f} ms) | "
                      f"verify level {sub['verify_level'] or 0}"]
            t = sub
            if t["t_signal"]:
                def d(x):
                    return f"{(x - t['t_signal']) * 1000:.0f}" if x else "—"
                lines.append(f"⏱ plan {d(t['t_planned'])} | ký {d(t['t_signed'])} | mô phỏng {d(t['t_simulated'])} | "
                             f"gửi {d(t['t_sent'])} | ACK {d(t['t_ack'])} | landed {d(t['t_landed'])} | "
                             f"xác minh {d(t['t_verified'])} ms")
            dec = a.engine._decimals(o["mint"])
            for tx in a.db.query("SELECT * FROM sub_txs WHERE submission_id=? ORDER BY idx", (sub["id"],)):
                lines.append(
                    f"\n#{tx['idx'] + 1} {tx['status']} <code>{tx['signature']}</code>\n"
                    f"  dự kiến: {fmt_amount(int(tx['planned_out_raw']), dec)} token, maxIn {sol_str(tx['max_in_lamports'])}"
                    f" | giá dự kiến {fmt_price(tx['planned_price'])}"
                    + (f"\n  thực tế: {fmt_amount(int(tx['actual_token_delta'] or 0), dec)} token, "
                       f"pool {sol_str(tx['swap_lamports'])} SOL, giá {fmt_price(tx['exec_price'])}, slot {tx['slot']}"
                       if tx["status"] == "SUCCESS" else "")
                    + (f"\n  lỗi: {esc(tx['err'])}" if tx["err"] else ""))
        await self.send(chat, "\n".join(lines), [[("📋 Lệnh", "ol"), ("📜 Lịch sử", "h")]])

    async def show_positions(self, chat: int, msg_id: int | None = None) -> None:
        a = self.app
        rows = a.positions.list_open()
        if not rows:
            return await self._render(chat, msg_id, "Không có vị thế mở.", [[("🏠 Menu", "m")]])
        lines = ["<b>Vị thế đang mở</b>"]
        kb = []
        for p in rows:
            snap = a.market.current(Pubkey.from_string(p["mint"]))
            tok = int(p["token_raw"])
            value = None
            if snap and snap.ps_state and tok > 0:
                try:
                    value = snap.ps_state.quote_sell(tok).user_out
                except Exception:
                    pass
            pnl = a.positions.pnl_pct(p["cost_lamports"], value) if value is not None else None
            lines.append(f"\n#{p['id']} {short(p['mint'])}: {fmt_amount(tok, p['decimals'] or 6)} token\n"
                         f"  Giá vốn {sol_str(p['cost_lamports'], 4)} SOL | Giá trị {sol_str(value, 4)} | "
                         f"PnL {pnl if pnl is not None else '—'}%\n"
                         f"  Đã chốt: {sol_str(p['realized_pnl_lamports'], 4)} SOL | TP {p['tp_pct'] or '—'}% "
                         f"SL {p['sl_pct'] or '—'}% {'✅' if p['tpsl_enabled'] else '⛔'} ({p['exit_state']})")
            kb.append([(f"#{p['id']} SELL 50%", f"ps:{p['id']}:50"), ("100%", f"ps:{p['id']}:100"),
                       ("TP/SL on/off", f"pt:{p['id']}")])
            kb.append([(f"Sửa TP #{p['id']}", f"ptp:{p['id']}"), (f"Sửa SL #{p['id']}", f"psl:{p['id']}")])
        kb.append([("🔄", "pl"), ("🏠 Menu", "m")])
        await self._render(chat, msg_id, "\n".join(lines), kb)

    async def show_history(self, chat: int, msg_id: int | None = None) -> None:
        a = self.app
        orders = a.db.query("SELECT * FROM orders ORDER BY id DESC LIMIT 12")
        sells = a.db.query("SELECT * FROM sells ORDER BY id DESC LIMIT 8")
        lines = ["<b>Lịch sử BUY</b>"]
        kb = []
        for o in orders:
            lines.append(f"#{o['id']} {o['kind']} ×{o['count']} {short(o['mint'])} {o['status']} ({o['exec_mode']})")
            kb.append((f"#{o['id']}", f"od:{o['id']}"))
        lines.append("\n<b>Lịch sử SELL</b>")
        for s in sells:
            lines.append(f"S{s['id']} {s['reason']} {s['pct']}% {short(s['mint'])} {s['status']} "
                         f"PnL {sol_str(s['pnl_lamports'], 4)}")
        rows = [kb[i:i + 4] for i in range(0, len(kb), 4)]
        rows.append([("🏠 Menu", "m")])
        await self._render(chat, msg_id, "\n".join(lines), rows)

    async def show_holds(self, chat: int, msg_id: int | None = None) -> None:
        rows = self.app.db.query("SELECT * FROM submissions WHERE status='UNKNOWN_HOLD' ORDER BY id")
        if not rows:
            return await self._render(chat, msg_id, "Không có giao dịch UNKNOWN/HOLD.", [[("🏠 Menu", "m")]])
        lines = ["<b>UNKNOWN/HOLD</b> — bot KHÔNG gửi lại; chỉ đối soát (không thể xóa ép)."]
        kb = []
        for r in rows:
            lines.append(f"#{r['id']} {r['purpose']} {short(r['mint'])} ×{r['count']} lastValid {r['last_valid_block_height']}")
            kb.append([(f"🔄 Kiểm tra lại #{r['id']}", f"hr:{r['id']}")])
        kb.append([("🏠 Menu", "m")])
        await self._render(chat, msg_id, "\n".join(lines), kb)

    async def show_settings(self, chat: int, msg_id: int | None = None) -> None:
        st = self.app.settings
        lines = ["<b>Cài đặt</b>"]
        kb = []
        for key, spec in SPECS.items():
            if key in ("mode", "paused"):
                continue
            lines.append(f"{esc(spec.label)}: <b>{esc(st.raw(key))}</b>")
            kb.append((spec.label[:22], f"se:{key}"))
        rows = [kb[i:i + 2] for i in range(0, len(kb), 2)]
        rows.append([("🏠 Menu", "m")])
        await self._render(chat, msg_id, "\n".join(lines), rows)

    async def show_stats(self, chat: int, msg_id: int | None = None) -> None:
        db = self.app.db
        lines = ["<b>Hiệu năng (200 mẫu gần nhất)</b>"]
        for name, label in (("prepare_ms", "Tín hiệu→mô phỏng xong"), ("simulate_ms", "Mô phỏng"),
                            ("jito_ack_ms", "Jito ACK")):
            vals = sorted(r["value_ms"] for r in db.query(
                "SELECT value_ms FROM metrics WHERE name=? ORDER BY id DESC LIMIT 200", (name,)))
            if vals:
                lines.append(f"{label}: p50 {vals[len(vals) // 2]:.0f} ms | p95 {vals[int(len(vals) * 0.95) - 1]:.0f} ms"
                             f" | n={len(vals)}")
        subs = db.query("SELECT t_signal,t_sent,t_landed,t_verified FROM submissions WHERE status='SUCCESS' "
                        "ORDER BY id DESC LIMIT 200")
        land = sorted((s["t_landed"] - s["t_sent"]) * 1000 for s in subs if s["t_landed"] and s["t_sent"])
        if land:
            lines.append(f"Gửi→landed: p50 {land[len(land) // 2]:.0f} ms | n={len(land)}")
        if len(lines) == 1:
            lines.append("Chưa có dữ liệu.")
        await self._render(chat, msg_id, "\n".join(lines), [[("🏠 Menu", "m")]])

    # ----------------------------------------------------------------------- mode --
    async def toggle_mode(self, chat: int, msg_id: int | None) -> None:
        st = self.app.settings
        if st.mode == MODE_LIVE:
            st.set_mode(MODE_NO_SEND, confirmed_by_user=True)
            await self.send(chat, "🧪 Đã chuyển sang NO_SEND. Không giao dịch nào được gửi.")
            return await self.show_menu(chat, msg_id)
        if not self.app.config.allow_live:
            return await self.send(chat, "🔒 LIVE bị khóa: operator phải đặt ALLOW_LIVE=true trên VPS rồi khởi động lại.")
        phrase = f"LIVE {str(self.app.wallet.pubkey)[-4:]}"
        self.session(chat).awaiting = ("live", phrase)
        await self.send(chat, f"⚠️ Bật LIVE sẽ gửi giao dịch THẬT.\nGõ chính xác: <code>{phrase}</code> để xác nhận.")

    async def confirm_live(self, chat: int, text: str) -> None:
        phrase = f"LIVE {str(self.app.wallet.pubkey)[-4:]}"
        if text.strip() != phrase:
            return await self.send(chat, "❌ Sai cụm xác nhận. Vẫn ở NO_SEND.")
        try:
            self.app.settings.set_mode(MODE_LIVE, confirmed_by_user=True)
        except SettingsError as e:
            return await self.send(chat, f"❌ {esc(e)}")
        await self.send(chat, "🟢 Đã bật LIVE.")
        await self.show_menu(chat)
