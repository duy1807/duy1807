"""In-memory Solana emulator used by the integration tests.

It decodes the *real signed bytes* the bot produces (signature check, blockhash validity, account
order, instruction data) and executes: ComputeBudget, System transfer, ATA create-idempotent,
SPL SyncNative / CloseAccount and PumpSwap buy_v2 / sell_v2 with on-chain guards
(max_quote_amount_in / min_quote_amount_out, required accounts, PDAs). It serves JSON-RPC
(getMultipleAccounts, getLatestBlockhash, simulateTransaction, simulateBundle, getSignatureStatuses,
getTransaction, ...) and the Jito block-engine API through an ``httpx.MockTransport``.
"""
from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass

import httpx
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.token.associated import get_associated_token_address
from solders.transaction import VersionedTransaction

from solbot import constants as C
from solbot.dex import pumpswap as ps

RENT = C.TOKEN_ACCOUNT_RENT_LAMPORTS
UVA_RENT = 1_500_000


class TxFail(Exception):
    pass


@dataclass
class Acc:
    lamports: int
    owner: Pubkey
    data: bytes = b""


def token_data(mint: Pubkey, owner: Pubkey, amount: int) -> bytes:
    d = bytearray(165)
    d[0:32] = bytes(mint)
    d[32:64] = bytes(owner)
    d[64:72] = amount.to_bytes(8, "little")
    d[108] = 1  # initialized
    return bytes(d)


def mint_data(supply: int, decimals: int) -> bytes:
    d = bytearray(82)
    d[36:44] = supply.to_bytes(8, "little")
    d[44] = decimals
    d[45] = 1
    return bytes(d)


def global_config_data(lp=20, proto=5, creator=5, buyback=None) -> bytes:
    from solbot.dex.pumpswap import DISC_GLOBAL_CONFIG

    d = bytearray(DISC_GLOBAL_CONFIG)
    d += bytes(32) + lp.to_bytes(8, "little") + proto.to_bytes(8, "little") + bytes([0])
    d += bytes(32 * 8) + creator.to_bytes(8, "little")
    d += bytes(32 * 3) + bytes([0]) + bytes(32 * 7) + bytes([0])
    for k in (buyback or C.BUYBACK_FEE_RECIPIENTS):
        d += bytes(k)
    d += (0).to_bytes(8, "little") + bytes(32) + bytes([0, 0]) + (0).to_bytes(8, "little")
    return bytes(d)


def tok_amount(a: Acc) -> int:
    return int.from_bytes(a.data[64:72], "little")


def set_amount(a: Acc, amount: int) -> None:
    d = bytearray(a.data)
    d[64:72] = amount.to_bytes(8, "little")
    a.data = bytes(d)


class FakeChain:
    def __init__(self, fee_config_b64: str, token_program: Pubkey = C.TOKEN_PROGRAM, decimals: int = 6,
                 base_reserve: int = 200_000_000_000_000, quote_reserve: int = 85_000_000_000,
                 supply: int = 1_000_000_000_000_000):
        self.accounts: dict[Pubkey, Acc] = {}
        self.slot = 400_000_000
        self.block_height = 380_000_000
        self.blockhashes: dict[str, int] = {}
        self.txs: dict[str, dict] = {}
        self.statuses: dict[str, dict] = {}
        self.jito_mode = "land"
        self.jito_calls: list[dict] = []
        self.rpc_fail: dict[str, str] = {}  # host -> "error" | "429" | "timeout"
        self.supports_simulate_bundle = True
        self.before_land_hook = None  # callable() executed right before a Jito landing (front-run)
        self.jito_429_budget = 0
        self.rpc_calls: dict[str, int] = {}
        self.rpc_slot_lag: dict[str, int] = {}  # host -> slots behind (stale node)
        self.rpc_fail_method: dict[tuple[str, str], str] = {}  # (host, method) -> "429"
        self.token_program = token_program
        self.decimals = decimals
        # programs/configs
        self.accounts[C.PUMP_AMM_GLOBAL_CONFIG] = Acc(1_000_000, C.PUMP_AMM_PROGRAM, global_config_data())
        self.accounts[C.PUMP_AMM_FEE_CONFIG] = Acc(1_000_000, C.PUMP_FEE_PROGRAM, base64.b64decode(fee_config_b64))
        for r in C.BUYBACK_FEE_RECIPIENTS:
            a = get_associated_token_address(r, C.WSOL_MINT, C.TOKEN_PROGRAM)
            self.accounts[a] = Acc(RENT, C.TOKEN_PROGRAM, token_data(C.WSOL_MINT, r, 0))
        # token + pool
        self.mint = Keypair().pubkey()
        self.accounts[self.mint] = Acc(1_461_600, token_program, mint_data(supply, decimals))
        self.pool_addr = C.canonical_pump_pool(self.mint)
        self.base_vault = get_associated_token_address(self.pool_addr, self.mint, token_program)
        self.quote_vault = get_associated_token_address(self.pool_addr, C.WSOL_MINT, C.TOKEN_PROGRAM)
        self.pool = ps.PoolState(
            address=self.pool_addr, pool_bump=255, index=0, creator=C.pump_pool_authority(self.mint),
            base_mint=self.mint, quote_mint=C.WSOL_MINT, lp_mint=Keypair().pubkey(),
            pool_base_token_account=self.base_vault, pool_quote_token_account=self.quote_vault, lp_supply=1,
            coin_creator=Keypair().pubkey(), is_mayhem_mode=False, is_cashback_coin=False,
            virtual_quote_reserves=0, creator_fee_bps=0, can_edit_creator_fee=False, is_holder_reward=False,
            protocol_fees=0, creator_fees=0)
        self.accounts[self.pool_addr] = Acc(5_000_000, C.PUMP_AMM_PROGRAM, ps.encode_pool(self.pool))
        self.accounts[self.base_vault] = Acc(RENT, token_program, token_data(self.mint, self.pool_addr, base_reserve))
        self.accounts[self.quote_vault] = Acc(RENT + quote_reserve, C.TOKEN_PROGRAM,
                                              token_data(C.WSOL_MINT, self.pool_addr, quote_reserve))
        self.trader = Keypair()  # external market participant
        self.fund(self.trader.pubkey(), 10_000 * 10**9)
        self.new_blockhash()

    # ------------------------------------------------------------------ helpers --
    def fund(self, who: Pubkey, lamports: int) -> None:
        a = self.accounts.get(who)
        if a is None:
            self.accounts[who] = Acc(lamports, C.SYSTEM_PROGRAM)
        else:
            a.lamports += lamports

    def new_blockhash(self) -> tuple[str, int]:
        h = str(Hash.new_unique())
        self.blockhashes[h] = self.block_height + 150
        self.latest = h
        return h, self.blockhashes[h]

    def advance(self, blocks: int = 1) -> None:
        self.block_height += blocks
        self.slot += blocks

    def token_balance(self, owner: Pubkey, mint: Pubkey | None = None) -> int:
        mint = mint or self.mint
        prog = self.token_program if mint == self.mint else C.TOKEN_PROGRAM
        a = self.accounts.get(get_associated_token_address(owner, mint, prog))
        return tok_amount(a) if a else 0

    def pool_state(self) -> ps.PumpSwapState:
        pool = ps.decode_pool(self.pool_addr, self.accounts[self.pool_addr].data)
        return ps.PumpSwapState(
            pool=pool, base_reserve=tok_amount(self.accounts[self.base_vault]),
            quote_vault=tok_amount(self.accounts[self.quote_vault]),
            base_mint_supply=int.from_bytes(self.accounts[self.mint].data[36:44], "little"),
            base_decimals=self.decimals, base_token_program=self.token_program, quote_token_program=C.TOKEN_PROGRAM,
            global_config=ps.decode_global_config(self.accounts[C.PUMP_AMM_GLOBAL_CONFIG].data),
            fee_config=ps.decode_fee_config(self.accounts[C.PUMP_AMM_FEE_CONFIG].data))

    def spot_price(self):
        return self.pool_state().spot_price_sol()

    def external_trade(self, side: str, lamports: int | None = None, base: int | None = None) -> None:
        """A third party trades directly against the pool (moves the price)."""
        st = self.pool_state()
        if side == "buy":
            cost = st.quote_buy_for_sol(lamports)
            self._apply_pool_buy(st, cost)
        else:
            out = st.quote_sell(base)
            self._apply_pool_sell(st, out)
        self.advance(1)

    def move_price_to(self, target_ratio: float, step: int = 50) -> None:
        """Sell (ratio<1) or buy (ratio>1) until spot price ~ ratio * current."""
        p0 = float(self.spot_price())
        for _ in range(20000):
            p = float(self.spot_price())
            if (target_ratio < 1 and p <= p0 * target_ratio) or (target_ratio > 1 and p >= p0 * target_ratio):
                return
            st = self.pool_state()
            if target_ratio < 1:
                self.external_trade("sell", base=st.base_reserve // step)
            else:
                self.external_trade("buy", lamports=st.effective_quote // step)

    def _apply_pool_buy(self, st, cost) -> None:
        new = st.after_buy(cost)
        self._write_pool(new)

    def _apply_pool_sell(self, st, out) -> None:
        new = st.after_sell(out)
        self._write_pool(new)

    def _write_pool(self, new: ps.PumpSwapState) -> None:
        self.accounts[self.pool_addr].data = ps.encode_pool(new.pool)
        bv, qv = self.accounts[self.base_vault], self.accounts[self.quote_vault]
        set_amount(bv, new.base_reserve)
        dq = new.quote_vault - tok_amount(qv)
        set_amount(qv, new.quote_vault)
        qv.lamports += dq

    # --------------------------------------------------------------- execution --
    def _copy(self):
        return {k: Acc(v.lamports, v.owner, v.data) for k, v in self.accounts.items()}

    def execute(self, raw: bytes, *, commit: bool, sig_verify: bool = True, charge_fee_on_fail: bool = False,
                state: dict | None = None) -> dict:
        tx = VersionedTransaction.from_bytes(raw)
        msg = tx.message
        keys = list(msg.account_keys)
        sig = str(tx.signatures[0])
        accs = state if state is not None else self._copy()
        pre = [accs[k].lamports if k in accs else 0 for k in keys]
        pre_tok = self._tok_balances(keys, accs)
        result = {"sig": sig, "err": None, "logs": [], "units": 0, "keys": keys}
        payer = keys[0]
        n_sig = msg.header.num_required_signatures
        cu_limit, micro = 200_000, 0
        try:
            if sig_verify and not all(tx.verify_with_results()):
                raise TxFail("SignatureFailure")
            if str(msg.recent_blockhash) not in self.blockhashes or \
                    self.blockhashes[str(msg.recent_blockhash)] < self.block_height:
                raise TxFail("BlockhashNotFound")
            if sig in self.txs:
                raise TxFail("AlreadyProcessed")
            for ix in msg.instructions:
                prog = keys[ix.program_id_index]
                if prog == C.COMPUTE_BUDGET_PROGRAM:
                    d = bytes(ix.data)
                    if d[0] == 2:
                        cu_limit = int.from_bytes(d[1:5], "little")
                    elif d[0] == 3:
                        micro = int.from_bytes(d[1:9], "little")
            fee = 5000 * n_sig + (cu_limit * micro + 999_999) // 1_000_000
            if payer not in accs or accs[payer].lamports < fee:
                raise TxFail("InsufficientFundsForFee")
            accs[payer].lamports -= fee
            result["fee"] = fee
            units = 0
            for ix in msg.instructions:
                units += self._exec_ix(keys, ix, accs, msg, result)
            if units > cu_limit:
                raise TxFail("ComputationalBudgetExceeded")
            result["units"] = units
        except TxFail as e:
            result["err"] = {"InstructionError": [0, {"Custom": str(e)}]} if str(e) not in (
                "BlockhashNotFound", "SignatureFailure", "AlreadyProcessed", "InsufficientFundsForFee") else str(e)
            result.setdefault("fee", 0)
            if charge_fee_on_fail and result["fee"]:
                fresh = self._copy() if state is None else state
                fresh[payer].lamports -= result["fee"]
                accs = fresh
        result["post"] = [accs[k].lamports if k in accs else 0 for k in keys]
        result["pre"] = pre
        result["pre_tok"] = pre_tok
        result["post_tok"] = self._tok_balances(keys, accs)
        result["state"] = accs
        if commit and (result["err"] is None or charge_fee_on_fail):
            self.accounts = accs
        return result

    def _tok_balances(self, keys, accs) -> list[dict]:
        out = []
        for i, k in enumerate(keys):
            a = accs.get(k)
            if a and a.owner in (C.TOKEN_PROGRAM, C.TOKEN_2022_PROGRAM) and len(a.data) >= 165:
                mint = Pubkey.from_bytes(a.data[0:32])
                owner = Pubkey.from_bytes(a.data[32:64])
                out.append({"accountIndex": i, "mint": str(mint), "owner": str(owner),
                            "uiTokenAmount": {"amount": str(tok_amount(a)), "decimals": self.decimals}})
        return out

    def _exec_ix(self, keys, ix, accs, msg, result) -> int:
        prog = keys[ix.program_id_index]
        ak = [keys[i] for i in ix.accounts]
        d = bytes(ix.data)
        signer = lambda k: msg.is_signer(keys.index(k))
        if prog == C.COMPUTE_BUDGET_PROGRAM:
            return 150
        if prog == C.SYSTEM_PROGRAM:
            if int.from_bytes(d[0:4], "little") != 2:
                raise TxFail("UnsupportedSystemIx")
            amt = int.from_bytes(d[4:12], "little")
            src, dst = ak[0], ak[1]
            if not signer(src):
                raise TxFail("MissingSignature")
            if accs[src].lamports < amt:
                raise TxFail("InsufficientFunds")
            accs[src].lamports -= amt
            accs.setdefault(dst, Acc(0, C.SYSTEM_PROGRAM)).lamports += amt
            return 150
        if prog == C.ASSOCIATED_TOKEN_PROGRAM:
            payer, ata, owner, mint, _sys, tprog = ak[:6]
            if get_associated_token_address(owner, mint, tprog) != ata:
                raise TxFail("InvalidATA")
            if ata in accs and accs[ata].lamports > 0:
                return 3000
            if accs[payer].lamports < RENT:
                raise TxFail("InsufficientFundsForRent")
            accs[payer].lamports -= RENT
            accs[ata] = Acc(RENT, tprog, token_data(mint, owner, 0))
            return 20000
        if prog in (C.TOKEN_PROGRAM, C.TOKEN_2022_PROGRAM):
            if d[0] == 17:  # SyncNative
                a = accs[ak[0]]
                if Pubkey.from_bytes(a.data[0:32]) != C.WSOL_MINT:
                    raise TxFail("NonNativeNotSupported")
                set_amount(a, a.lamports - RENT)
                return 3000
            if d[0] == 9:  # CloseAccount
                a = accs[ak[0]]
                native = Pubkey.from_bytes(a.data[0:32]) == C.WSOL_MINT
                if not native and tok_amount(a) != 0:
                    raise TxFail("NonNativeHasBalance")
                if Pubkey.from_bytes(a.data[32:64]) != ak[2] or not signer(ak[2]):
                    raise TxFail("OwnerMismatch")
                accs[ak[1]].lamports += a.lamports
                del accs[ak[0]]
                return 3000
            raise TxFail("UnsupportedTokenIx")
        if prog == C.PUMP_AMM_PROGRAM:
            return self._exec_amm(ak, d, accs, signer)
        if prog == C.JUPITER_V6_PROGRAM:
            return self._exec_jup(ak, d, accs, signer)
        raise TxFail(f"UnknownProgram {prog}")

    def _exec_jup(self, ak, d, accs, signer) -> int:
        """Emulated Jupiter v6 ExactIn route through the PumpSwap pool, honouring the encoded minimum."""
        user, src, dst = ak[0], ak[1], ak[2]
        if not signer(user):
            raise TxFail("MissingSigner")
        tail = d[-19:]
        in_amount = int.from_bytes(tail[0:8], "little")
        quoted_out = int.from_bytes(tail[8:16], "little")
        slip = int.from_bytes(tail[16:18], "little")
        min_out = quoted_out * (10_000 - slip) // 10_000
        saved = self.accounts
        self.accounts = accs
        try:
            st = self.pool_state()
            s_acc, d_acc = accs[src], accs.get(dst)
            if d_acc is None:
                raise TxFail("AccountNotInitialized(dst)")
            if Pubkey.from_bytes(s_acc.data[0:32]) == C.WSOL_MINT:
                cost = st.quote_buy_for_sol(in_amount)
                if cost.base_out < min_out:
                    raise TxFail("SlippageToleranceExceeded")
                if tok_amount(s_acc) < cost.total:
                    raise TxFail("InsufficientQuote")
                set_amount(s_acc, tok_amount(s_acc) - cost.total)
                s_acc.lamports -= cost.total
                set_amount(d_acc, tok_amount(d_acc) + cost.base_out)
                self._write_pool(st.after_buy(cost))
            else:
                out = st.quote_sell(in_amount)
                if out.user_out < min_out:
                    raise TxFail("SlippageToleranceExceeded")
                if tok_amount(s_acc) < in_amount:
                    raise TxFail("InsufficientBase")
                set_amount(s_acc, tok_amount(s_acc) - in_amount)
                self._write_pool(st.after_sell(out))
                set_amount(d_acc, tok_amount(d_acc) + out.user_out)
                d_acc.lamports += out.user_out
        except ps.QuoteError as e:
            raise TxFail(f"Math: {e}")
        finally:
            self.accounts = saved
        return 120_000

    def _exec_amm(self, ak, d, accs, signer) -> int:
        kind, amount, limit = ps.decode_trade_v2(d)
        (pool_k, user, gcfg, base_mint, quote_mint, user_base, user_quote, pool_base, pool_quote, base_tp, quote_tp,
         sysp, uva, fcfg, buyback, evt, program) = ak
        if pool_k != self.pool_addr or gcfg != C.PUMP_AMM_GLOBAL_CONFIG or fcfg != C.PUMP_AMM_FEE_CONFIG:
            raise TxFail("ConstraintSeeds")
        if evt != C.PUMP_AMM_EVENT_AUTHORITY or program != C.PUMP_AMM_PROGRAM or sysp != C.SYSTEM_PROGRAM:
            raise TxFail("ConstraintAddress")
        if not signer(user):
            raise TxFail("MissingSigner")
        if pool_base != self.base_vault or pool_quote != self.quote_vault or base_mint != self.mint \
                or quote_mint != C.WSOL_MINT or base_tp != self.token_program or quote_tp != C.TOKEN_PROGRAM:
            raise TxFail("ConstraintTokenMint")
        if uva != C.user_volume_accumulator(user):
            raise TxFail("ConstraintSeeds(uva)")
        if buyback not in accs:
            raise TxFail("AccountNotInitialized(buyback)")
        if user_base not in accs or user_quote not in accs:
            raise TxFail("AccountNotInitialized(user token account)")
        if uva not in accs:
            accs[user].lamports -= UVA_RENT
            accs[uva] = Acc(UVA_RENT, C.PUMP_AMM_PROGRAM, b"uva")
        # build state from the *transaction's* account view
        saved = self.accounts
        self.accounts = accs
        try:
            st = self.pool_state()
            if kind == "buy_v2":
                cost = st.cost_for_base(amount)
                if cost.total > limit:
                    raise TxFail("ExceededSlippage(max_quote_amount_in)")
                uq = accs[user_quote]
                if tok_amount(uq) < cost.total:
                    raise TxFail("InsufficientQuote")
                set_amount(uq, tok_amount(uq) - cost.total)
                uq.lamports -= cost.total
                set_amount(accs[user_base], tok_amount(accs[user_base]) + amount)
                self._write_pool(st.after_buy(cost))
            else:
                out = st.quote_sell(amount)
                if out.user_out < limit:
                    raise TxFail("ExceededSlippage(min_quote_amount_out)")
                ub = accs[user_base]
                if tok_amount(ub) < amount:
                    raise TxFail("InsufficientBase")
                set_amount(ub, tok_amount(ub) - amount)
                self._write_pool(st.after_sell(out))
                uq = accs[user_quote]
                set_amount(uq, tok_amount(uq) + out.user_out)
                uq.lamports += out.user_out
        except ps.QuoteError as e:
            raise TxFail(f"Math: {e}")
        finally:
            self.accounts = saved
        return 60_000

    def land(self, res: dict, bundle_id: str | None = None) -> None:
        self.slot += 1
        self.txs[res["sig"]] = {
            "slot": self.slot, "blockTime": 0,
            "meta": {"err": res["err"], "fee": res.get("fee", 0), "preBalances": res["pre"],
                     "postBalances": res["post"], "preTokenBalances": res["pre_tok"],
                     "postTokenBalances": res["post_tok"], "loadedAddresses": {"writable": [], "readonly": []},
                     "logMessages": []},
            "transaction": {"message": {"accountKeys": [str(k) for k in res["keys"]]},
                            "signatures": [res["sig"]]},
            "version": 0,
        }
        self.statuses[res["sig"]] = {"slot": self.slot, "confirmations": 1, "err": res["err"],
                                     "confirmationStatus": "confirmed"}

    # ------------------------------------------------------------------- RPC --
    def handle_rpc(self, host: str, body: dict):
        method = body["method"]
        params = body.get("params") or []
        self.rpc_calls[method] = self.rpc_calls.get(method, 0) + 1
        ctx = {"slot": self.slot - self.rpc_slot_lag.get(host, 0)}
        if method == "getMultipleAccounts":
            vals = []
            for k in params[0]:
                a = self.accounts.get(Pubkey.from_string(k))
                vals.append(None if a is None else {"lamports": a.lamports, "owner": str(a.owner),
                                                    "data": [base64.b64encode(a.data).decode(), "base64"],
                                                    "executable": False, "rentEpoch": 0})
            return {"context": ctx, "value": vals}
        if method == "getBalance":
            a = self.accounts.get(Pubkey.from_string(params[0]))
            return {"context": ctx, "value": a.lamports if a else 0}
        if method == "getLatestBlockhash":
            if self.blockhashes[self.latest] - 150 < self.block_height - 10:
                self.new_blockhash()  # like a real chain: a fresh blockhash every block
            return {"context": ctx, "value": {"blockhash": self.latest,
                                              "lastValidBlockHeight": self.blockhashes[self.latest]}}
        if method == "getBlockHeight":
            return self.block_height
        if method == "getSlot":
            return self.slot
        if method == "getSignatureStatuses":
            return {"context": ctx, "value": [self.statuses.get(s) for s in params[0]]}
        if method == "getTransaction":
            return self.txs.get(params[0])
        if method == "simulateTransaction":
            raw = base64.b64decode(params[0])
            cfg = params[1] if len(params) > 1 else {}
            if cfg.get("minContextSlot") and cfg["minContextSlot"] > self.slot - self.rpc_slot_lag.get(host, 0):
                return {"__error__": {"code": -32016, "message": "Minimum context slot has not been reached"}}
            res = self.execute(raw, commit=False, sig_verify=cfg.get("sigVerify", False))
            accounts = None
            if cfg.get("accounts"):
                accounts = [self._acc_json(res["state"].get(Pubkey.from_string(a)))
                            for a in cfg["accounts"]["addresses"]]
            return {"context": ctx, "value": {"err": res["err"], "logs": [], "accounts": accounts,
                                              "unitsConsumed": res["units"]}}
        if method == "simulateBundle":
            if not self.supports_simulate_bundle:
                return {"__error__": {"code": -32601, "message": "Method not found"}}
            txs = [base64.b64decode(x) for x in params[0]["encodedTransactions"]]
            if not txs or len(params) < 2:
                return {"__error__": {"code": -32602, "message": "invalid params: empty bundle"}}
            cfg = params[1]
            state = self._copy()
            results = []
            summary = "succeeded"
            for i, raw in enumerate(txs):
                res = self.execute(raw, commit=False, sig_verify=not cfg.get("skipSigVerify"), state=state)
                post_cfg = cfg["postExecutionAccountsConfigs"][i]
                post = None
                if post_cfg:
                    post = [self._acc_json(res["state"].get(Pubkey.from_string(a))) for a in post_cfg["addresses"]]
                results.append({"err": res["err"], "logs": [], "preExecutionAccounts": None,
                                "postExecutionAccounts": post, "unitsConsumed": res["units"], "returnData": None})
                if res["err"] is not None:
                    summary = {"failed": {"error": res["err"], "tx_signature": res["sig"]}}
                    break
                state = res["state"]
            return {"context": ctx, "value": {"summary": summary, "transactionResults": results}}
        if method == "getProgramAccounts":
            return []
        return {"__error__": {"code": -32601, "message": f"Method not found: {method}"}}

    def _acc_json(self, a: Acc | None):
        if a is None:
            return None
        return {"lamports": a.lamports, "owner": str(a.owner), "data": [base64.b64encode(a.data).decode(), "base64"],
                "executable": False, "rentEpoch": 0}

    # ------------------------------------------------------------------- Jito --
    def handle_jito(self, path: str, body: dict):
        method = body["method"]
        self.jito_calls.append({"method": method, "path": path})
        if method == "getTipAccounts":
            return {"result": [str(x) for x in C.JITO_TIP_ACCOUNTS]}
        if method == "getInflightBundleStatuses":
            return {"result": {"value": []}}
        if self.jito_429_budget > 0:
            self.jito_429_budget -= 1
            return {"__http__": 429}
        if self.jito_mode == "http500":
            return {"__http__": 500}
        if method == "sendTransaction":
            raws = [base64.b64decode(body["params"][0])]
        else:
            raws = [base64.b64decode(x) for x in body["params"][0]]
        sigs = [str(VersionedTransaction.from_bytes(r).signatures[0]) for r in raws]
        bundle_id = uuid.uuid4().hex
        if self.jito_mode == "drop":
            pass
        else:
            if self.before_land_hook:
                self.before_land_hook()
            if self.jito_mode == "land_err" and len(raws) == 1:
                res = self.execute(raws[0], commit=True, charge_fee_on_fail=True)
                if res["err"] is None:
                    pass
                self.land(res)
            else:
                state = self._copy()
                results = []
                ok = True
                for r in raws:
                    if VersionedTransaction.from_bytes(r).signatures[0].__str__() in self.txs:
                        ok = False  # duplicate signature: cannot execute twice
                        break
                    res = self.execute(r, commit=False, state=state)
                    if res["err"] is not None:
                        ok = False
                        break
                    state = res["state"]
                    results.append(res)
                if ok:  # atomic: all land or none
                    self.accounts = state
                    for res in results:
                        self.land(res, bundle_id)
        self.advance(1)
        if method == "sendTransaction":
            return {"result": sigs[0], "__headers__": {"x-bundle-id": bundle_id}}
        return {"result": bundle_id}

    # ---------------------------------------------------------------- Jupiter --
    jup_malicious = False

    def handle_jupiter(self, request: httpx.Request) -> httpx.Response:
        import hashlib
        from solbot.tx_builder import ata, close_account, create_ata_idempotent, sol_transfer, sync_native

        self.rpc_calls["jup:" + request.url.path] = self.rpc_calls.get("jup:" + request.url.path, 0) + 1
        st = self.pool_state()
        if request.url.path.endswith("/quote"):
            q = request.url.params
            amount = int(q["amount"])
            slip = int(q["slippageBps"])
            if q["inputMint"] == str(C.WSOL_MINT):
                out = st.quote_buy_for_sol(amount).base_out
            else:
                out = st.quote_sell(amount).user_out
            return httpx.Response(200, json={
                "inputMint": q["inputMint"], "outputMint": q["outputMint"], "inAmount": str(amount),
                "outAmount": str(out), "otherAmountThreshold": str(out * (10_000 - slip) // 10_000),
                "swapMode": "ExactIn", "slippageBps": slip, "priceImpactPct": "0.01",
                "routePlan": [{"swapInfo": {"ammKey": str(self.pool_addr)}, "percent": 100}],
                "contextSlot": self.slot})
        body = json.loads(request.content)
        qr = body["quoteResponse"]
        user = Pubkey.from_string(body["userPublicKey"])
        in_mint = Pubkey.from_string(qr["inputMint"])
        out_mint = Pubkey.from_string(qr["outputMint"])
        in_amount = int(qr["inAmount"])
        quoted, slip = int(qr["outAmount"]), int(qr["slippageBps"])
        if self.jup_malicious:
            slip = 10_000  # ignores the requested minimum
        prog = lambda m: self.token_program if m == self.mint else C.TOKEN_PROGRAM
        src, dst = ata(user, in_mint, prog(in_mint)), ata(user, out_mint, prog(out_mint))
        setup = [create_ata_idempotent(user, user, out_mint, prog(out_mint))]
        cleanup = []
        if in_mint == C.WSOL_MINT:
            setup += [create_ata_idempotent(user, user, C.WSOL_MINT), sol_transfer(user, src, in_amount),
                      sync_native(src)]
            cleanup = [close_account(src, user, user)]
        else:
            cleanup = [close_account(dst, user, user)]
        data = hashlib.sha256(b"global:route").digest()[:8] + bytes(8) + in_amount.to_bytes(8, "little") \
            + quoted.to_bytes(8, "little") + slip.to_bytes(2, "little") + bytes([0])
        from solders.instruction import AccountMeta, Instruction
        swap = Instruction(C.JUPITER_V6_PROGRAM, data, [
            AccountMeta(user, True, True), AccountMeta(src, False, True), AccountMeta(dst, False, True),
            AccountMeta(self.pool_addr, False, True), AccountMeta(self.base_vault, False, True),
            AccountMeta(self.quote_vault, False, True)])

        def ij(ix):
            return {"programId": str(ix.program_id), "data": base64.b64encode(bytes(ix.data)).decode(),
                    "accounts": [{"pubkey": str(a.pubkey), "isSigner": a.is_signer, "isWritable": a.is_writable}
                                 for a in ix.accounts]}
        return httpx.Response(200, json={
            "computeBudgetInstructions": [], "setupInstructions": [ij(x) for x in setup],
            "swapInstruction": ij(swap), "cleanupInstruction": ij(cleanup[0]) if cleanup else None,
            "otherInstructions": [], "addressLookupTableAddresses": []})

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            body = json.loads(request.content or b"{}")
            mode = self.rpc_fail.get(host) or self.rpc_fail_method.get((host, body.get("method")))
            if mode == "429":
                return httpx.Response(429, headers={"retry-after": "0"})
            if mode == "error":
                return httpx.Response(503)
            if mode == "timeout":
                raise httpx.ReadTimeout("timeout", request=request)
            if "block-engine" in host or host.startswith("jito"):
                res = self.handle_jito(request.url.path, body)
                if "__http__" in res:
                    return httpx.Response(res["__http__"], json={"error": "x"})
                headers = res.pop("__headers__", {})
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), **res}, headers=headers)
            if "jup" in host:
                return self.handle_jupiter(request)
            res = self.handle_rpc(host, body)
            if isinstance(res, dict) and "__error__" in res:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "error": res["__error__"]})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": res})

        return httpx.MockTransport(handler)
