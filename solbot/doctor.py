"""`python -m solbot doctor` — pre-flight check of a deployment (no mint needed, nothing sent).

Checks configuration, wallet file permissions/decryption and balance, every RPC (latency, slot spread,
health), the two verification RPCs, simulateBundle support, Jito Tokyo, the Yellowstone gRPC stream
(including token rejection), Telegram getMe, the database and the LIVE gate.
Exit code 0 = no FAIL.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
import time

import httpx

from .config import Config
from .db import Database
from .rpc import HttpJsonRpc, RpcError, RpcMethodUnsupported
from .settings import Settings

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


class Doctor:
    def __init__(self, config: Config):
        self.c = config
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, status: str, detail: str) -> None:
        self.rows.append((name, status, detail))
        print(f"[{status}] {name}: {detail}", flush=True)

    # ----------------------------------------------------------------------------------------
    def check_config(self) -> None:
        c = self.c
        self.add("RPC_URLS", PASS if c.rpc_urls else FAIL, f"{len(c.rpc_urls)} endpoint(s)")
        both = bool(c.verify_rpc_a_url and c.verify_rpc_b_url)
        indep = both and _host(c.verify_rpc_a_url) != _host(c.verify_rpc_b_url)
        self.add("Verify RPC A/B độc lập", PASS if indep else WARN,
                 "khác nhà cung cấp" if indep else "nên đặt VERIFY_RPC_A_URL / VERIFY_RPC_B_URL ở 2 nhà cung cấp khác nhau")
        self.add("BUNDLE_SIM_RPC_URL", PASS if c.bundle_sim_rpc_url else WARN,
                 "đã đặt" if c.bundle_sim_rpc_url else "thiếu → Bundle ×3/×5 sẽ bị từ chối ở LIVE")
        self.add("Telegram cấu hình", PASS if (c.telegram_token and c.telegram_allowed_user_ids) else FAIL,
                 f"allowed users: {len(c.telegram_allowed_user_ids)}" if c.telegram_token else "thiếu TELEGRAM_BOT_TOKEN")
        self.add("GRPC_ENDPOINT", PASS if (c.grpc_endpoint and c.grpc_token) else WARN,
                 (f"{c.grpc_endpoint.split(':')[0]} token={'có' if c.grpc_token else 'KHÔNG'}") if c.grpc_endpoint
                 else "không có gRPC → HTTP polling")
        self.add("ALLOW_LIVE", PASS, f"{c.allow_live} (LIVE còn cần xác nhận trong Telegram)")

    def check_files(self) -> None:
        c = self.c
        path = c.wallet_encrypted_path or c.wallet_keypair_path
        if path is None:
            self.add("Ví", FAIL, "chưa đặt WALLET_ENCRYPTED_PATH hoặc WALLET_KEYPAIR_PATH")
            return
        if not path.exists():
            self.add("Ví", FAIL, f"không thấy file {path}")
            return
        mode = path.stat().st_mode
        self.add("Quyền file ví", PASS if not mode & (stat.S_IRWXG | stat.S_IRWXO) else FAIL, oct(mode & 0o777))
        env = Path(".env")
        if env.exists():
            m = env.stat().st_mode
            self.add("Quyền .env", PASS if not m & (stat.S_IRWXG | stat.S_IRWXO) else WARN, oct(m & 0o777))
        try:
            c.db_path.parent.mkdir(parents=True, exist_ok=True)
            db = Database(c.db_path)
            s = Settings(db, c)
            self.add("Database", PASS, f"{c.db_path} (mode hiện tại: {s.mode})")
            db.close()
        except Exception as e:
            self.add("Database", FAIL, str(e))
        free = shutil.disk_usage(c.db_path.parent).free / 1e9
        self.add("Dung lượng đĩa trống", PASS if free > 1 else WARN, f"{free:.1f} GB")

    async def check_wallet(self, rpc: HttpJsonRpc | None) -> None:
        from .wallet import WalletError, load_wallet

        c = self.c
        if not (c.wallet_encrypted_path or c.wallet_keypair_path):
            return
        if c.wallet_encrypted_path and not os.environ.get(c.wallet_passphrase_env) and not sys.stdin.isatty():
            self.add("Giải mã ví", SKIP, f"{c.wallet_passphrase_env} không có và không có terminal để nhập passphrase")
            return  # in a terminal load_wallet() asks for the passphrase interactively
        try:
            w = load_wallet(c.wallet_keypair_path, c.wallet_encrypted_path, c.wallet_passphrase_env)
        except (WalletError, Exception) as e:
            self.add("Giải mã ví", FAIL, str(e))
            return
        self.add("Ví", PASS, str(w.pubkey))
        if rpc:
            try:
                bal = (await rpc.call("getBalance", [str(w.pubkey)]))["value"]
                self.add("Số dư ví", PASS if bal > 0 else WARN, f"{bal / 1e9:.6f} SOL")
            except Exception as e:
                self.add("Số dư ví", WARN, str(e))

    async def check_rpcs(self) -> HttpJsonRpc | None:
        c = self.c
        first = None
        slots = {}
        urls = list(dict.fromkeys(c.rpc_urls + [u for u in (c.sim_rpc_url or "").split(",") if u.strip()]
                                  + [c.verify_rpc_a_url, c.verify_rpc_b_url]))
        for u in [u for u in urls if u]:
            ep = HttpJsonRpc(u, c.rpc_timeout_ms)
            try:
                await ep.call("getSlot", [{"commitment": "processed"}])  # connect
                t0 = time.perf_counter()
                s = await ep.call("getSlot", [{"commitment": "processed"}])
                ms = (time.perf_counter() - t0) * 1000
                health = "?"
                try:
                    health = await ep.call("getHealth", [])
                except RpcError as e:
                    health = e.message[:40]
                slots[ep.name] = s
                self.add(f"RPC {ep.name}", PASS if ms < 150 else WARN, f"slot {s}, {ms:.0f} ms (ấm), health={health}")
                first = first or ep
            except Exception as e:
                self.add(f"RPC {ep.name}", FAIL, f"{type(e).__name__}: {e}")
        if len(slots) > 1:
            spread = max(slots.values()) - min(slots.values())
            self.add("Lệch slot giữa các RPC", PASS if spread <= c.rpc_max_slot_lag else WARN, f"{spread} slot")
        if c.bundle_sim_rpc_url:
            ep = HttpJsonRpc(c.bundle_sim_rpc_url, c.rpc_timeout_ms)
            try:
                await ep.call("simulateBundle", [{"encodedTransactions": []}])
                self.add("simulateBundle", PASS, f"{ep.name} hỗ trợ")
            except RpcMethodUnsupported:
                self.add("simulateBundle", FAIL, f"{ep.name} KHÔNG hỗ trợ → Bundle ×3/×5 bị từ chối ở LIVE")
            except RpcError as e:
                self.add("simulateBundle", PASS, f"{ep.name} hỗ trợ ({e.message[:60]})")
            except Exception as e:
                self.add("simulateBundle", WARN, str(e))
        return first

    async def check_jito(self) -> None:
        url = self.c.jito_url.rstrip("/") + "/api/v1/bundles"
        body = {"jsonrpc": "2.0", "id": 1, "method": "getTipAccounts", "params": []}
        async with httpx.AsyncClient(timeout=5) as cl:
            try:
                await cl.post(url, json=body)
                t0 = time.perf_counter()
                r = await cl.post(url, json=body)
                ms = (time.perf_counter() - t0) * 1000
                ok = r.status_code == 200 and len(r.json().get("result") or []) > 0
                self.add("Jito", PASS if ok and ms < 100 else (WARN if ok else FAIL),
                         f"{_host(self.c.jito_url)} RTT {ms:.0f} ms (VPS Tokyo nên < 10 ms)")
            except Exception as e:
                self.add("Jito", FAIL, f"{type(e).__name__}: {e}")

    async def check_grpc(self) -> None:
        c = self.c
        if not c.grpc_endpoint:
            self.add("Yellowstone gRPC", SKIP, "không cấu hình")
            return
        import grpc

        from .grpc_gen import geyser_pb2 as pb
        from .grpc_gen import geyser_pb2_grpc as pbg

        roots = c.grpc_root_certs.read_bytes() if c.grpc_root_certs else None
        ch = (grpc.aio.secure_channel(c.grpc_endpoint, grpc.ssl_channel_credentials(root_certificates=roots))
              if c.grpc_tls else grpc.aio.insecure_channel(c.grpc_endpoint))
        md = (("x-token", c.grpc_token),) if c.grpc_token else None

        async def reqs():
            req = pb.SubscribeRequest()
            req.slots["hb"].CopyFrom(pb.SubscribeRequestFilterSlots(filter_by_commitment=True))
            req.commitment = pb.CommitmentLevel.PROCESSED
            yield req
            await asyncio.sleep(10)

        n, t0 = 0, time.monotonic()
        try:
            call = pbg.GeyserStub(ch).Subscribe(reqs(), metadata=md)
            async for u in call:
                if u.WhichOneof("update_oneof") == "slot":
                    n += 1
                if time.monotonic() - t0 > 3:
                    call.cancel()
                    break
            self.add("Yellowstone gRPC", PASS if n >= 5 else WARN, f"{n} slot update / 3 s")
        except grpc.aio.AioRpcError as e:
            if e.code() == grpc.StatusCode.CANCELLED and n:
                self.add("Yellowstone gRPC", PASS, f"{n} slot update / 3 s")
            else:
                self.add("Yellowstone gRPC", FAIL, f"{e.code().name}: {e.details()} (bot sẽ dùng HTTP polling)")
        except Exception as e:
            self.add("Yellowstone gRPC", FAIL, f"{type(e).__name__}: {e}")
        finally:
            await ch.close()

    async def check_telegram(self) -> None:
        c = self.c
        if not c.telegram_token:
            self.add("Telegram getMe", SKIP, "chưa có token")
            return
        try:
            async with httpx.AsyncClient(timeout=10) as cl:
                r = await cl.get(f"{c.telegram_api_base}/bot{c.telegram_token}/getMe")
                j = r.json()
            if j.get("ok"):
                self.add("Telegram getMe", PASS, f"@{j['result'].get('username')}")
            else:
                self.add("Telegram getMe", FAIL, str(j.get("description"))[:100])
        except Exception as e:
            self.add("Telegram getMe", FAIL, type(e).__name__)

    async def run(self) -> int:
        self.check_config()
        self.check_files()
        first = await self.check_rpcs()
        await self.check_wallet(first)
        await self.check_jito()
        await self.check_grpc()
        await self.check_telegram()
        fails = sum(1 for r in self.rows if r[1] == FAIL)
        warns = sum(1 for r in self.rows if r[1] == WARN)
        print(f"\n== {len(self.rows)} kiểm tra: {fails} FAIL, {warns} WARN ==")
        return 1 if fails else 0


def _host(u: str | None) -> str:
    return (u or "").split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]


from pathlib import Path  # noqa: E402  (used in check_files)
