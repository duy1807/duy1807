#!/usr/bin/env python3
"""Wallet keystore tool. Never prints the secret key.

  python scripts/wallet_tool.py new --out secrets/wallet.enc.json
  python scripts/wallet_tool.py import --keypair ~/id.json --out secrets/wallet.enc.json
  python scripts/wallet_tool.py pubkey --keystore secrets/wallet.enc.json
"""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solders.keypair import Keypair  # noqa: E402

from solbot.wallet import decrypt_keystore, encrypt_keypair, load_keypair_json  # noqa: E402


def _write_private(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=1)


def _ask_pass() -> str:
    a = getpass.getpass("Passphrase (>=12 ký tự): ")
    b = getpass.getpass("Nhập lại: ")
    if a != b:
        sys.exit("Passphrase không khớp")
    return a


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("--out", required=True)
    i = sub.add_parser("import")
    i.add_argument("--keypair", required=True)
    i.add_argument("--out", required=True)
    p = sub.add_parser("pubkey")
    p.add_argument("--keystore", required=True)
    a = ap.parse_args()
    if a.cmd == "new":
        kp = Keypair()
        _write_private(Path(a.out), encrypt_keypair(kp, _ask_pass()))
        print(f"Đã tạo keystore {a.out}\nPubkey: {kp.pubkey()}")
    elif a.cmd == "import":
        kp = load_keypair_json(Path(a.keypair))
        _write_private(Path(a.out), encrypt_keypair(kp, _ask_pass()))
        print(f"Đã mã hóa keystore {a.out}\nPubkey: {kp.pubkey()}\nHãy xóa an toàn file keypair gốc nếu không cần.")
    else:
        doc = json.loads(Path(a.keystore).read_text())
        kp = decrypt_keystore(doc, getpass.getpass("Passphrase: "))
        print(kp.pubkey())


if __name__ == "__main__":
    main()
