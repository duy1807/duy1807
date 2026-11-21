"""Telegram controller flows against a fake Bot API + the fake chain."""
import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from solbot.app import App
from solbot.settings import MODE_LIVE
from solbot.wallet import Wallet

from .conftest import make_config, wait_for

USER = 111
CHAT = 111
TOKEN = "123456:TEST-TOKEN-SECRET-VALUE"


class FakeTelegram:
    def __init__(self, chain):
        self.chain = chain
        self.sent: list[dict] = []
        self.msg_id = 0
        base = chain.transport()
        self._base = base

    def transport(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.telegram.org":
                method = request.url.path.rsplit("/", 1)[-1]
                body = json.loads(request.content or b"{}")
                if method in ("sendMessage", "editMessageText"):
                    self.msg_id += 1
                    self.sent.append({"method": method, **body})
                    return httpx.Response(200, json={"ok": True, "result": {"message_id": self.msg_id}})
                if method == "getUpdates":
                    return httpx.Response(200, json={"ok": True, "result": []})
                return httpx.Response(200, json={"ok": True, "result": True})
            return self._base.handle_request(request)

        return httpx.MockTransport(handler)

    def texts(self):
        return [m.get("text", "") for m in self.sent]

    def last(self):
        return self.sent[-1]

    def buttons(self, m=None):
        m = m or self.last()
        rm = m.get("reply_markup") or {}
        return [b["callback_data"] for row in rm.get("inline_keyboard", []) for b in row]

    def find_button(self, prefix):
        for m in reversed(self.sent):
            for b in self.buttons(m):
                if b.startswith(prefix):
                    return b
        raise AssertionError(f"no button {prefix}")


@pytest.fixture
async def tg(tmp_path, chain):
    from solders.keypair import Keypair

    kp = Keypair()
    chain.fund(kp.pubkey(), 100 * 10**9)
    fake = FakeTelegram(chain)
    cfg = make_config(tmp_path, telegram_token=TOKEN, telegram_allowed_user_ids=[USER])
    app = App(cfg, wallet=Wallet(kp), transport=fake.transport(), enable_telegram=True)
    await app.start()
    fake.app = app
    fake.kp = kp
    yield fake
    await app.stop()


def msg(text, uid=USER):
    return {"update_id": 1, "message": {"chat": {"id": CHAT}, "from": {"id": uid}, "text": text}}


def cb(data, uid=USER):
    return {"update_id": 2, "callback_query": {"id": "q", "from": {"id": uid}, "data": data,
                                               "message": {"chat": {"id": CHAT}, "message_id": 1}}}


async def send(tg, u):
    await tg.app.telegram.handle_update(u)
    await asyncio.sleep(0.02)


async def test_unauthorized_user_is_ignored(tg):
    await asyncio.sleep(0.05)
    n = len(tg.sent)  # startup notice to the allowed chat
    await send(tg, msg("/menu", uid=999))
    await send(tg, cb("b:N:1", uid=999))
    assert len(tg.sent) == n
    assert not tg.app.db.query("SELECT * FROM orders")


async def test_token_card_has_all_nine_buy_buttons_and_sell(tg):
    await send(tg, msg(str(tg.chain.mint)))
    card = tg.last()
    assert "Giá:" in card["text"] and "Market Cap" in card["text"] and "Thanh khoản" in card["text"]
    btns = tg.buttons()
    for k in ("N", "D", "L"):
        for c in (1, 3, 5):
            assert f"b:{k}:{c}" in btns
    for pct in ("25", "50", "75", "100"):
        assert f"s:{pct}" in btns
    assert "sc" in btns and "p:sol" in btns and "p:tp" in btns and "p:sl" in btns and "p:dip" in btns


async def test_buy_now_x3_via_telegram_live(tg):
    tg.app.settings.set_mode(MODE_LIVE, confirmed_by_user=True)
    await send(tg, msg(str(tg.chain.mint)))
    await send(tg, cb("p:sol"))
    await send(tg, msg("0.05"))
    await send(tg, cb("b:N:3"))
    assert "BUY NGAY ×3" in tg.last()["text"] and "0.05 × 3" in tg.last()["text"]
    await send(tg, cb(tg.find_button("cf:")))
    o = await wait_for(lambda: tg.app.db.one("SELECT * FROM orders WHERE status='FILLED'"))
    assert o["count"] == 3 and o["sol_per_buy_lamports"] == 50_000_000 and o["chat_id"] == CHAT
    await wait_for(lambda: any("BUY NOW ×3" in t for t in tg.texts()))
    report = [t for t in tg.texts() if "BUY NOW ×3" in t][0]
    for needle in ("SOL thực chi", "Token thực nhận", "Giá thực hiện", "TP/SL", "SUCCESS", "Thời gian xử lý"):
        assert needle in report


async def test_limit_requires_price_then_arm_list_and_cancel(tg):
    await send(tg, msg(str(tg.chain.mint)))
    await send(tg, cb("b:L:5"))
    assert "Nhập giá LIMIT" in tg.last()["text"]
    price = tg.chain.spot_price() * Decimal("0.8")
    await send(tg, msg(f"{price:.15f}"))
    await send(tg, cb("b:L:5"))
    await send(tg, cb(tg.find_button("cf:")))
    o = tg.app.db.one("SELECT * FROM orders WHERE kind='LIMIT'")
    assert o["status"] == "ARMED" and o["count"] == 5
    await send(tg, cb("ol"))
    t = tg.last()["text"]
    assert f"#{o['id']}" in t and "Kích hoạt" in t and "Hiện tại" in t and "Tham chiếu" in t
    assert f"oc:{o['id']}" in tg.buttons()
    await send(tg, cb(f"oc:{o['id']}"))
    assert tg.app.db.get("orders", o["id"])["status"] == "CANCELLED"


async def test_dip_x5_via_telegram_then_sell_and_bundle_detail(tg):
    app = tg.app
    app.settings.set_mode(MODE_LIVE, confirmed_by_user=True)
    await send(tg, msg(str(tg.chain.mint)))
    await send(tg, cb("p:dip"))
    await send(tg, msg("8"))
    await send(tg, cb("b:D:5"))
    assert "DIP 8%" in tg.last()["text"]
    await send(tg, cb(tg.find_button("cf:")))
    o = app.db.one("SELECT * FROM orders WHERE kind='DIP'")
    assert o["status"] == "ARMED" and o["dip_pct"] == "8"
    tg.chain.move_price_to(0.85)
    await wait_for(lambda: app.db.get("orders", o["id"])["status"] == "FILLED")
    await send(tg, cb(f"od:{o['id']}"))
    detail = tg.last()["text"]
    assert detail.count("SUCCESS") >= 5 and "Bundle" in detail and "⏱" in detail
    await send(tg, cb("s:50"))
    await send(tg, cb(tg.find_button("cf:")))
    await wait_for(lambda: app.db.one("SELECT * FROM sells WHERE status='FILLED'"))
    await wait_for(lambda: any("SELL 50" in t and "PnL thực hiện" in t for t in tg.texts()))
    await send(tg, cb("pl"))
    assert "Vị thế đang mở" in tg.last()["text"]


async def test_live_toggle_requires_phrase(tg):
    app = tg.app
    await send(tg, cb("md"))
    assert "Gõ chính xác" in tg.last()["text"]
    await send(tg, msg("LIVE"))
    assert app.settings.mode == "NO_SEND"
    await send(tg, cb("md"))
    await send(tg, msg(f"LIVE {str(tg.kp.pubkey())[-4:]}"))
    assert app.settings.mode == "LIVE"
    await send(tg, cb("md"))
    assert app.settings.mode == "NO_SEND"


async def test_live_locked_without_operator_flag(tg):
    tg.app.config.allow_live = False
    await send(tg, cb("md"))
    assert "LIVE bị khóa" in tg.last()["text"]
    assert tg.app.settings.mode == "NO_SEND"


async def test_settings_validation_and_stale_confirm(tg):
    await send(tg, cb("se:slippage_pct"))
    await send(tg, msg("150"))
    assert "tối đa" in tg.last()["text"]
    await send(tg, cb("se:slippage_pct"))
    await send(tg, msg("12"))
    assert tg.app.settings.raw("slippage_pct") == "12"
    await send(tg, cb("cf:deadbeef"))
    assert "hết hạn" in tg.last()["text"]


async def test_no_secret_ever_sent(tg):
    tg.app.settings.set_mode(MODE_LIVE, confirmed_by_user=True)
    await send(tg, msg("/menu"))
    await send(tg, msg(str(tg.chain.mint)))
    await send(tg, cb("b:N:1"))
    await send(tg, cb(tg.find_button("cf:")))
    await wait_for(lambda: tg.app.db.one("SELECT * FROM orders WHERE status='FILLED'"))
    await asyncio.sleep(0.1)
    blob = json.dumps(tg.sent)
    assert TOKEN not in blob
    assert str(tg.kp) not in blob
    assert json.dumps(list(bytes(tg.kp))) not in blob
