"""Wallet loading. The secret key never leaves this module except as a signer object.

Two storage formats are supported:

* ``WALLET_KEYPAIR_PATH``: solana-keygen JSON array. The file must not be readable by group/other.
* ``WALLET_ENCRYPTED_PATH``: keystore produced by ``scripts/wallet_tool.py`` (scrypt + AES-256-GCM).
  The passphrase comes from the ``WALLET_PASSPHRASE`` env var (e.g. a systemd credential) or an
  interactive prompt.

Every textual form of the secret is registered with :mod:`solbot.logging_setup` so it is
redacted from logs and Telegram output even if some code path accidentally formats it.
"""
from __future__ import annotations

import base64
import getpass
import json
import os
import stat
from pathlib import Path

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from . import logging_setup


class WalletError(RuntimeError):
    pass


class Wallet:
    def __init__(self, keypair: Keypair):
        self._kp = keypair
        self.pubkey: Pubkey = keypair.pubkey()
        sk = bytes(keypair)
        logging_setup.register_secret(str(keypair))  # base58 64-byte secret
        logging_setup.register_secret(json.dumps(list(sk)))
        logging_setup.register_secret(json.dumps(list(sk), separators=(",", ":")))
        logging_setup.register_secret(base64.b64encode(sk).decode())
        logging_setup.register_secret(sk.hex())

    @property
    def signer(self) -> Keypair:
        return self._kp

    def __repr__(self) -> str:  # never print the secret
        return f"Wallet({self.pubkey})"

    __str__ = __repr__


def _check_private_file(path: Path) -> None:
    st = path.stat()
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise WalletError(
            f"{path} có quyền truy cập quá rộng; chạy: chmod 600 {path}"
        )


def load_keypair_json(path: Path) -> Keypair:
    _check_private_file(path)
    data = json.loads(path.read_text())
    if not isinstance(data, list) or len(data) != 64:
        raise WalletError("File keypair phải là mảng JSON 64 byte (solana-keygen)")
    return Keypair.from_bytes(bytes(data))


# --- encrypted keystore -----------------------------------------------------------------------

def _kdf(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode())


def encrypt_keypair(kp: Keypair, passphrase: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(passphrase) < 12:
        raise WalletError("Passphrase tối thiểu 12 ký tự")
    salt = os.urandom(16)
    nonce = os.urandom(12)
    n, r, p = 2**15, 8, 1
    key = _kdf(passphrase, salt, n, r, p)
    ct = AESGCM(key).encrypt(nonce, bytes(kp), str(kp.pubkey()).encode())
    return {
        "version": 1,
        "pubkey": str(kp.pubkey()),
        "kdf": {"name": "scrypt", "n": n, "r": r, "p": p, "salt": base64.b64encode(salt).decode()},
        "cipher": {"name": "aes-256-gcm", "nonce": base64.b64encode(nonce).decode()},
        "ciphertext": base64.b64encode(ct).decode(),
    }


def decrypt_keystore(doc: dict, passphrase: str) -> Keypair:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    kdf = doc["kdf"]
    key = _kdf(passphrase, base64.b64decode(kdf["salt"]), kdf["n"], kdf["r"], kdf["p"])
    try:
        sk = AESGCM(key).decrypt(
            base64.b64decode(doc["cipher"]["nonce"]),
            base64.b64decode(doc["ciphertext"]),
            doc["pubkey"].encode(),
        )
    except Exception:
        raise WalletError("Sai passphrase hoặc keystore hỏng")
    kp = Keypair.from_bytes(sk)
    if str(kp.pubkey()) != doc["pubkey"]:
        raise WalletError("Keystore không khớp pubkey")
    return kp


def load_wallet(keypair_path: Path | None, encrypted_path: Path | None,
                passphrase_env: str = "WALLET_PASSPHRASE") -> Wallet:
    if encrypted_path:
        _check_private_file(encrypted_path)
        doc = json.loads(encrypted_path.read_text())
        pw = os.environ.pop(passphrase_env, None)  # remove from env after reading
        if pw is None:
            pw = getpass.getpass("Wallet passphrase: ")
        logging_setup.register_secret(pw)
        return Wallet(decrypt_keystore(doc, pw))
    if keypair_path:
        return Wallet(load_keypair_json(keypair_path))
    raise WalletError("Chưa cấu hình WALLET_KEYPAIR_PATH hoặc WALLET_ENCRYPTED_PATH")
