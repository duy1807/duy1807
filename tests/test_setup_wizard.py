"""scripts/setup_wizard.py driven end-to-end with scripted answers (no network, no systemd)."""
import builtins
import getpass
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest
from solders.keypair import Keypair

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("setup_wizard", ROOT / "scripts/setup_wizard.py")
wiz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wiz)

HELIUS_KEY = "11111111-2222-3333-4444-555555555555"
TG_TOKEN = "123456789:AAH-secret-telegram-token-value"
GRPC_TOKEN = "grpc-secret-token-value-xyz"
PASS = "correct horse battery staple"


def test_rpc_url_from_key_or_url():
    assert wiz.rpc_url_from(HELIUS_KEY) == f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
    url = "https://mainnet.helius-rpc.com/?api-key=x&rebate-address=y"
    assert wiz.rpc_url_from(url) == url
    with pytest.raises(ValueError):
        wiz.rpc_url_from("not a key")


def test_enable_credential_in_unit(tmp_path):
    unit = tmp_path / "solbot.service"
    unit.write_text((ROOT / "deploy/solbot.service").read_text().replace(
        "LoadCredentialEncrypted=", "#LoadCredentialEncrypted="))
    assert wiz.enable_credential_in_unit(unit)
    assert "\nLoadCredentialEncrypted=wallet_pass:" in unit.read_text()


def _run(monkeypatch, tmp_path, capsys, answers, secrets, tg_updates):
    app = tmp_path / "app"
    app.mkdir()
    (app / ".env").write_text((ROOT / ".env.example").read_text())
    inputs = iter(answers)
    hidden = iter(secrets)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(inputs))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(hidden))
    monkeypatch.setattr(wiz, "_rpc_ping", lambda url: (True, "slot 1, 5 ms"))

    def fake_tg(token, method, **params):
        assert token == TG_TOKEN
        if method == "getMe":
            return {"ok": True, "result": {"username": "my_solbot"}}
        return {"ok": True, "result": tg_updates}

    monkeypatch.setattr(wiz, "telegram", fake_tg)
    calls = []
    monkeypatch.setattr(wiz, "run_as", lambda user, cmd, env, cwd: calls.append((cmd, env)) or 0)
    monkeypatch.setattr("sys.argv", ["setup_wizard.py", "--app-dir", str(app), "--no-systemd", "--user", ""])
    wiz.main()
    return app, calls, capsys.readouterr().out


def test_wizard_end_to_end_new_wallet(monkeypatch, tmp_path, capsys):
    updates = [{"update_id": 1, "message": {"from": {"id": 424242, "is_bot": False, "username": "duy"}}}]
    answers = [
        "",            # backup RPC -> default publicnode (template placeholder is never offered)
        "",            # Enter after sending /start
        "y",           # use detected id
        "n",           # import existing keypair? no -> new wallet
    ]
    secrets = [HELIUS_KEY, GRPC_TOKEN, TG_TOKEN, PASS, PASS]
    app, calls, out = _run(monkeypatch, tmp_path, capsys, answers, secrets, updates)
    env = wiz.read_env(app / ".env")
    helius = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
    assert env["VERIFY_RPC_A_URL"] == helius and env["BUNDLE_SIM_RPC_URL"] == helius
    assert env["RPC_URLS"].split(",")[0] == helius
    assert env["TELEGRAM_ALLOWED_USER_IDS"] == "424242"
    assert env["GRPC_TOKEN"] == GRPC_TOKEN and env["GRPC_ENDPOINT"] == wiz.PUBLICNODE_GRPC
    assert env["ALLOW_LIVE"] == "false"
    assert env["VERIFY_RPC_B_URL"] == wiz.PUBLICNODE_RPC and "example" not in (app / ".env").read_text()
    assert stat.S_IMODE(os.stat(app / ".env").st_mode) == 0o600
    ks = app / "secrets/wallet.enc.json"
    assert stat.S_IMODE(os.stat(ks).st_mode) == 0o600
    from solbot.wallet import decrypt_keystore

    kp = decrypt_keystore(json.loads(ks.read_text()), PASS)
    assert str(kp.pubkey()) in out
    # doctor was run with the passphrase passed only through the environment
    assert calls and calls[0][0][-2:] == ["solbot", "doctor"] and calls[0][1]["WALLET_PASSPHRASE"] == PASS
    # no secret is ever echoed
    for s in (HELIUS_KEY, TG_TOKEN, GRPC_TOKEN, PASS, str(kp)):
        assert s not in out


def test_generated_env_is_read_by_the_bot(monkeypatch, tmp_path, capsys):
    updates = [{"update_id": 1, "message": {"from": {"id": 7, "is_bot": False, "first_name": "D"}}}]
    app, _, _ = _run(monkeypatch, tmp_path, capsys, ["", "", "y", "n"], [HELIUS_KEY, "", TG_TOKEN, PASS, PASS],
                     updates)
    for k in list(os.environ):
        if k in ("RPC_URLS", "TELEGRAM_BOT_TOKEN", "GRPC_ENDPOINT", "ALLOW_LIVE", "WALLET_ENCRYPTED_PATH"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(app)
    from solbot.config import Config

    c = Config.from_env()
    assert len(c.rpc_urls) == 2 and c.telegram_allowed_user_ids == [7] and c.allow_live is False
    assert c.grpc_endpoint is None  # no gRPC token -> gRPC disabled, HTTP polling
    assert str(c.wallet_encrypted_path) == "secrets/wallet.enc.json"


def test_wizard_imports_existing_keypair(monkeypatch, tmp_path, capsys):
    kp = Keypair()
    src = tmp_path / "id.json"
    src.write_text(json.dumps(list(bytes(kp))))
    updates = [{"update_id": 1, "message": {"from": {"id": 9, "is_bot": False}}}]
    app, _, out = _run(monkeypatch, tmp_path, capsys, ["", "", "y", "y", str(src)],
                       [HELIUS_KEY, "", TG_TOKEN, PASS, PASS], updates)
    from solbot.wallet import decrypt_keystore

    assert decrypt_keystore(json.loads((app / "secrets/wallet.enc.json").read_text()), PASS).pubkey() == kp.pubkey()
    assert str(kp) not in out
