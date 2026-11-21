import json
import os
from decimal import Decimal

import pytest
from solders.keypair import Keypair

from solbot import constants as C
from solbot import logging_setup
from solbot.config import Config
from solbot.db import Database
from solbot.dex.jupiter import encoded_min_out
from solbot.engine import cap_lamports, min_tokens_for
from solbot.risk import RiskManager, RiskReject
from solbot.settings import MODE_LIVE, Settings, SettingsError
from solbot.verify import Verifier, parse_receipt
from solbot.wallet import WalletError, decrypt_keystore, encrypt_keypair, load_keypair_json, load_wallet


def test_wallet_file_permissions_enforced(tmp_path):
    kp = Keypair()
    p = tmp_path / "id.json"
    p.write_text(json.dumps(list(bytes(kp))))
    os.chmod(p, 0o644)
    with pytest.raises(WalletError):
        load_keypair_json(p)
    os.chmod(p, 0o600)
    assert load_keypair_json(p).pubkey() == kp.pubkey()


def test_encrypted_keystore_roundtrip(tmp_path, monkeypatch):
    kp = Keypair()
    doc = encrypt_keypair(kp, "correct horse battery staple")
    assert str(kp) not in json.dumps(doc)
    assert decrypt_keystore(doc, "correct horse battery staple").pubkey() == kp.pubkey()
    with pytest.raises(WalletError):
        decrypt_keystore(doc, "wrong passphrase!!")
    p = tmp_path / "w.enc.json"
    p.write_text(json.dumps(doc))
    os.chmod(p, 0o600)
    monkeypatch.setenv("WALLET_PASSPHRASE", "correct horse battery staple")
    w = load_wallet(None, p)
    assert w.pubkey == kp.pubkey() and "WALLET_PASSPHRASE" not in os.environ
    assert str(kp) not in repr(w)


def test_log_redaction(caplog):
    kp = Keypair()
    from solbot.wallet import Wallet

    Wallet(kp)
    logging_setup.register_secret("my-telegram-token-123456")
    assert "[REDACTED]" in logging_setup.redact(f"key={kp} tok=my-telegram-token-123456")
    assert str(kp) not in logging_setup.redact(f"oops {kp}")
    assert json.dumps(list(bytes(kp))) not in logging_setup.redact(json.dumps(list(bytes(kp))))
    assert "abc123" not in logging_setup.redact("https://rpc.example/?api-key=abc123")


def _settings(tmp_path, allow_live=False):
    c = Config()
    c.allow_live = allow_live
    return Settings(Database(tmp_path / "s.db"), c), c


def test_settings_defaults_and_validation(tmp_path):
    s, _ = _settings(tmp_path)
    assert s.mode == "NO_SEND"  # never LIVE by default
    with pytest.raises(SettingsError):
        s.set("slippage_pct", "101")
    with pytest.raises(SettingsError):
        s.set("buy_count", "2")
    with pytest.raises(SettingsError):
        s.set("sol_per_buy", "6")  # > HARD_MAX_SOL_PER_TX
    with pytest.raises(SettingsError):
        s.set("tip_sol", "0.0000001")  # < 1000 lamports
    assert s.set("slippage_pct", "100") == "1E+2" or s.dec("slippage_pct") == 100
    with pytest.raises(SettingsError):
        s.set("mode", "LIVE")  # mode only via set_mode


def test_live_requires_operator_flag_and_confirmation(tmp_path):
    s, c = _settings(tmp_path, allow_live=False)
    with pytest.raises(SettingsError):
        s.set_mode(MODE_LIVE, confirmed_by_user=True)
    c.allow_live = True
    with pytest.raises(SettingsError):
        s.set_mode(MODE_LIVE, confirmed_by_user=False)
    s.set_mode(MODE_LIVE, confirmed_by_user=True)
    assert s.is_live
    c.allow_live = False  # operator kill switch overrides the stored value
    assert s.mode == "NO_SEND"


def test_risk_balance_and_limits(tmp_path):
    s, c = _settings(tmp_path)
    r = RiskManager(s.db, s, c)
    kw = dict(mint="M", exec_mode="NO_SEND", count=3, sol_per_tx=10**8, max_ins=[105_000_000] * 3,
              slippage_bps=500, tip=10_000, micro_lamports=100_000, cu_limit=200_000, snapshot_age_ms=100,
              wallet_lamports=10**9, base_ata_exists=False, token_2022=False, uva_exists=False)
    f = r.check_buy(**kw)
    assert f.max_swap_in == 315_000_000 and f.rent == C.TOKEN_ACCOUNT_RENT_LAMPORTS + 3_000_000
    with pytest.raises(RiskReject) as e:
        r.check_buy(**{**kw, "wallet_lamports": 300_000_000})
    assert "Số dư không đủ" in str(e.value)
    with pytest.raises(RiskReject):
        r.check_buy(**{**kw, "snapshot_age_ms": 601})
    with pytest.raises(RiskReject):
        r.check_buy(**{**kw, "count": 2})
    with pytest.raises(RiskReject):
        r.check_buy(**{**kw, "exec_mode": "LIVE"})  # bot is NO_SEND
    with pytest.raises(RiskReject):
        r.check_buy(**{**kw, "tip": 500})


def test_price_cap_helpers_are_conservative():
    cap = Decimal("0.0000004")
    base = 1_234_567_891
    lam = cap_lamports(base, cap, 6)
    assert Decimal(lam) / base * 10**6 / 10**9 <= cap  # floor => never above the cap
    need = min_tokens_for(100_000_000, cap, 6)
    assert Decimal(100_000_000) / need * 10**6 / 10**9 <= cap  # ceil => never above the cap


def test_jupiter_encoded_min_out_parser():
    import hashlib
    from solders.instruction import Instruction

    data = hashlib.sha256(b"global:route").digest()[:8] + b"\x00" * 10 + (5000).to_bytes(8, "little") \
        + (1000).to_bytes(8, "little") + (100).to_bytes(2, "little") + b"\x00"
    ix = Instruction(C.JUPITER_V6_PROGRAM, data, [])
    assert encoded_min_out(ix, 5000) == 990
    assert encoded_min_out(ix, 4999) is None  # layout mismatch -> unverifiable
    assert encoded_min_out(Instruction(C.PUMP_AMM_PROGRAM, data, []), 5000) is None


def test_receipt_parsing_buy_with_rent_and_tip():
    w, mint, tip = Keypair().pubkey(), Keypair().pubkey(), C.JITO_TIP_ACCOUNTS[0]
    ata = Keypair().pubkey()
    tx = {"slot": 7, "meta": {"err": None, "fee": 25_000,
                              "preBalances": [1_000_000_000, 0, 5_000],
                              "postBalances": [1_000_000_000 - 100_000_000 - 25_000 - 10_000 - 2_039_280, 2_039_280,
                                               15_000],
                              "preTokenBalances": [],
                              "postTokenBalances": [{"accountIndex": 1, "mint": str(mint), "owner": str(w),
                                                     "uiTokenAmount": {"amount": "777"}}]},
          "transaction": {"message": {"accountKeys": [str(w), str(ata), str(tip)]}}}
    rc = parse_receipt("sig", tx, w, mint, tip)
    assert rc.ok and rc.token_delta == 777 and rc.tip_paid == 10_000 and rc.rent_created == 2_039_280
    assert rc.swap_lamports == 100_000_000 and rc.net_lamports == -(100_000_000 + 25_000 + 10_000)


def test_aggregate_statuses():
    agg = Verifier.aggregate
    assert agg(["SUCCESS", "SUCCESS"]) == "SUCCESS"
    assert agg(["FAILED_SAFE"]) == "FAILED_SAFE"
    assert agg(["SUCCESS", "UNKNOWN_HOLD"]) == "UNKNOWN_HOLD"
    assert agg(["PENDING", "SUCCESS"]) == "PENDING"


def test_rpc_never_exposes_send_transaction():
    from solbot.rpc import RpcPool

    assert not [m for m in dir(RpcPool) if "send" in m.lower()]
