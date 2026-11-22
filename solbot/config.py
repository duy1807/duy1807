"""Static infrastructure configuration (from environment / .env).

Trading parameters that the user tunes from Telegram live in the database
(see :mod:`solbot.settings`); this module only holds endpoints, paths and hard limits.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip()


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    v = _env(name)
    return default if v is None else int(v)


def _env_list(name: str) -> list[str]:
    v = _env(name)
    if not v:
        return []
    return [x.strip() for x in v.split(",") if x.strip()]


@dataclass
class Config:
    # --- storage / identity ---
    db_path: Path = Path("data/solbot.sqlite3")
    wallet_keypair_path: Path | None = None  # solana-keygen JSON (chmod 600)
    wallet_encrypted_path: Path | None = None  # encrypted keystore (scripts/wallet_tool.py)
    wallet_passphrase_env: str = "WALLET_PASSPHRASE"

    # --- telegram ---
    telegram_token: str | None = None
    telegram_allowed_user_ids: list[int] = field(default_factory=list)
    telegram_api_base: str = "https://api.telegram.org"

    # --- RPC endpoints (HTTP JSON-RPC). The bot NEVER sends transactions through these. ---
    rpc_urls: list[str] = field(default_factory=list)  # reads: accounts, balances, blockhash
    sim_rpc_url: str | None = None  # simulateTransaction; comma list = failover order (default: RPC_URLS)
    bundle_sim_rpc_url: str | None = None  # simulateBundle (Jito-Solana compatible RPC)
    verify_rpc_a_url: str | None = None  # independent verification RPC #1
    verify_rpc_b_url: str | None = None  # independent verification RPC #2
    rpc_timeout_ms: int = 2500
    rpc_max_slot_lag: int = 30  # a response whose context.slot lags the best known slot more is stale

    # --- Yellowstone gRPC ---
    grpc_endpoint: str | None = None  # host:port
    grpc_token: str | None = None  # x-token
    grpc_tls: bool = True
    grpc_commitment: str = "processed"
    grpc_stale_ms: int = 1500  # no stream message for this long => fall back to HTTP polling
    grpc_root_certs: Path | None = None  # custom CA bundle (only behind TLS-intercepting proxies)

    # --- Market data fallback (when gRPC is not configured or down) ---
    poll_interval_ms: int = 300

    # --- Jito ---
    jito_url: str = "https://tokyo.mainnet.block-engine.jito.wtf"
    jito_extra_urls: list[str] = field(default_factory=list)  # identical-bytes fan-out (optional)
    jito_uuid: str | None = None
    jito_timeout_ms: int = 2000
    jito_identical_resend: int = 2  # resend the SAME signed bytes on 429/connect error

    # --- Jupiter ---
    jupiter_base_url: str = "https://lite-api.jup.ag/swap/v1"
    jupiter_api_key: str | None = None
    jupiter_price_poll_ms: int = 800
    jupiter_max_accounts: int = 40

    # --- Safety (operator-level, cannot be changed from Telegram) ---
    allow_live: bool = False  # LIVE may only be switched on from Telegram if this is true
    snapshot_max_age_ms: int = 600
    trigger_snapshot_reuse_ms: int = 150  # older trigger snapshots are re-fetched before planning (≈1 RTT)
    trigger_rearm_cooldown_ms: int = 1000  # after a not-sent re-arm, wait before the order may fire again
    hard_max_sol_per_tx: float = 5.0
    hard_max_sol_per_order: float = 25.0
    hard_max_tip_sol: float = 0.05
    hard_max_priority_micro_lamports: int = 5_000_000
    blockhash_max_age_ms: int = 20_000
    verify_poll_ms: int = 400
    verify_deadline_s: float = 150.0  # then UNKNOWN/HOLD; the reconciler keeps checking
    verify_expiry_margin_blocks: int = 5
    reconcile_interval_ms: int = 3000
    keepalive_interval_s: float = 10.0  # ping every hot-path endpoint so connections stay warm
    sim_allow_missing_bundle_sim_in_no_send: bool = True

    log_level: str = "INFO"
    log_dir: Path = Path("logs")

    @property
    def primary_rpc(self) -> str:
        if not self.rpc_urls:
            raise RuntimeError("RPC_URLS is not configured")
        return self.rpc_urls[0]

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> "Config":
        """Read configuration from the process environment plus a .env file: ``env_file`` if given, else
        ./.env in the current directory, else the .env next to the installed package. Real environment
        variables always win over the file."""
        try:
            from dotenv import load_dotenv  # optional

            candidates = [Path(env_file)] if env_file else [Path.cwd() / ".env",
                                                            Path(__file__).resolve().parent.parent / ".env"]
            for p in candidates:
                if p.is_file():
                    load_dotenv(dotenv_path=p, override=False)
                    break
        except Exception:  # pragma: no cover
            pass
        c = cls()
        c.db_path = Path(_env("DB_PATH", str(c.db_path)))
        kp = _env("WALLET_KEYPAIR_PATH")
        c.wallet_keypair_path = Path(kp) if kp else None
        enc = _env("WALLET_ENCRYPTED_PATH")
        c.wallet_encrypted_path = Path(enc) if enc else None
        c.telegram_token = _env("TELEGRAM_BOT_TOKEN")
        c.telegram_allowed_user_ids = [int(x) for x in _env_list("TELEGRAM_ALLOWED_USER_IDS")]
        c.rpc_urls = _env_list("RPC_URLS")
        c.sim_rpc_url = _env("SIM_RPC_URL")
        c.bundle_sim_rpc_url = _env("BUNDLE_SIM_RPC_URL")
        c.verify_rpc_a_url = _env("VERIFY_RPC_A_URL")
        c.verify_rpc_b_url = _env("VERIFY_RPC_B_URL")
        c.rpc_timeout_ms = _env_int("RPC_TIMEOUT_MS", c.rpc_timeout_ms)
        c.rpc_max_slot_lag = _env_int("RPC_MAX_SLOT_LAG", c.rpc_max_slot_lag)
        c.grpc_endpoint = _env("GRPC_ENDPOINT")
        c.grpc_token = _env("GRPC_TOKEN")
        c.grpc_tls = _env_bool("GRPC_TLS", c.grpc_tls)
        c.grpc_commitment = _env("GRPC_COMMITMENT", c.grpc_commitment)
        c.grpc_stale_ms = _env_int("GRPC_STALE_MS", c.grpc_stale_ms)
        rc = _env("GRPC_ROOT_CERTS")
        c.grpc_root_certs = Path(rc) if rc else None
        c.poll_interval_ms = _env_int("POLL_INTERVAL_MS", c.poll_interval_ms)
        c.jito_url = _env("JITO_URL", c.jito_url)
        c.jito_extra_urls = _env_list("JITO_EXTRA_URLS")
        c.jito_uuid = _env("JITO_UUID")
        c.jito_timeout_ms = _env_int("JITO_TIMEOUT_MS", c.jito_timeout_ms)
        c.jito_identical_resend = _env_int("JITO_IDENTICAL_RESEND", c.jito_identical_resend)
        c.jupiter_base_url = _env("JUPITER_BASE_URL", c.jupiter_base_url)
        c.jupiter_api_key = _env("JUPITER_API_KEY")
        c.jupiter_price_poll_ms = _env_int("JUPITER_PRICE_POLL_MS", c.jupiter_price_poll_ms)
        c.allow_live = _env_bool("ALLOW_LIVE", False)
        c.snapshot_max_age_ms = _env_int("SNAPSHOT_MAX_AGE_MS", c.snapshot_max_age_ms)
        if c.snapshot_max_age_ms > 600:
            # The specification caps snapshot age at 600 ms; never allow looser.
            c.snapshot_max_age_ms = 600
        c.hard_max_sol_per_tx = float(_env("HARD_MAX_SOL_PER_TX", str(c.hard_max_sol_per_tx)))
        c.hard_max_sol_per_order = float(_env("HARD_MAX_SOL_PER_ORDER", str(c.hard_max_sol_per_order)))
        c.hard_max_tip_sol = float(_env("HARD_MAX_TIP_SOL", str(c.hard_max_tip_sol)))
        c.log_level = _env("LOG_LEVEL", c.log_level)
        c.log_dir = Path(_env("LOG_DIR", str(c.log_dir)))
        return c

    def describe(self) -> dict:
        """Non-secret summary for diagnostics (never includes tokens or keys)."""

        def host(u: str | None) -> str | None:
            if not u:
                return None
            return u.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]

        return {
            "rpc": [host(u) for u in self.rpc_urls],
            "sim_rpc": [host(u) for u in (self.sim_rpc_url or "").split(",") if u.strip()],
            "bundle_sim_rpc": host(self.bundle_sim_rpc_url),
            "verify_a": host(self.verify_rpc_a_url),
            "verify_b": host(self.verify_rpc_b_url),
            "grpc": self.grpc_endpoint.split(":")[0] if self.grpc_endpoint else None,
            "jito": host(self.jito_url),
            "allow_live": self.allow_live,
            "snapshot_max_age_ms": self.snapshot_max_age_ms,
        }
