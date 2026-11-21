#!/usr/bin/env python3
"""Run the whole test suite and write docs/ACCEPTANCE_REPORT.md (PASS/FAIL per requirement + evidence)."""
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQS = [
    ("1. Hạ tầng, Telegram, ví, database", [
        ("Ví: private key không lộ (file 600, keystore mã hóa, che log/Telegram)",
         ["test_wallet_file_permissions_enforced", "test_encrypted_keystore_roundtrip", "test_log_redaction",
          "test_no_secret_ever_sent"]),
        ("RPC: HTTP 429, timeout, failover, stale slot", ["test_rpc_429_failover", "test_stale_rpc_node_is_skipped"]),
        ("RPC không bao giờ gửi giao dịch (không fallback public RPC)", ["test_rpc_never_exposes_send_transaction"]),
        ("Mô phỏng: 429 → chuyển endpoint; mọi endpoint 429 → DIP về ARMED, không bị đốt",
         ["test_simulation_rate_limit_fails_over_and_never_burns_dip"]),
        ("Mô phỏng không bao giờ chạy trên bank cũ hơn snapshot (minContextSlot, -32016 → node khác)",
         ["test_simulation_never_runs_on_a_bank_older_than_the_snapshot"]),
        ("gRPC bị từ chối (thiếu/sai token) → tự chuyển HTTP polling, DIP/LIMIT vẫn hoạt động",
         ["test_grpc_rejected_falls_back_to_http_polling"]),
        ("Yellowstone gRPC: subscribe, heartbeat ≤600 ms, gom theo chữ ký, mất kết nối/resync",
         ["test_geyser_stream_snapshots_heartbeat_and_dip", "test_geyser_disconnect_marks_stale_and_resyncs"]),
        ("PumpSwap: discriminator/IDL, thứ tự 17 account, PDA, toán phí khớp SDK chính thức (golden)",
         ["test_instruction_discriminators_match_idl_and_anchor_hash", "test_v2_account_order_matches_idl",
          "test_known_pdas", "test_decode_mainnet_fee_config", "test_buy_base_input_golden_flat_fees",
          "test_sell_base_input_golden_flat_fees", "test_sell_mainnet_fee_tier_goldens",
          "test_buy_quote_roundtrip_never_exceeds_budget", "test_sequential_legs_raise_price",
          "test_pool_decode_roundtrip_and_short_layout"]),
        ("Cài đặt: NO_SEND mặc định, kiểm tra miền giá trị + giới hạn cứng",
         ["test_settings_defaults_and_validation", "test_settings_validation_and_stale_confirm"]),
        ("LIVE chỉ bật khi ALLOW_LIVE=true + người dùng gõ xác nhận",
         ["test_live_requires_operator_flag_and_confirmation", "test_live_toggle_requires_phrase",
          "test_live_locked_without_operator_flag"]),
        ("Telegram: chỉ user được phép; giao diện token đủ 9 nút BUY + SELL + tham số",
         ["test_unauthorized_user_is_ignored", "test_token_card_has_all_nine_buy_buttons_and_sell"]),
    ]),
    ("2. BUY ×1 và SELL", [
        ("BUY NGAY ×1 (PumpSwap) gửi Jito sendTransaction, xác minh on-chain, tạo vị thế",
         ["test_buy_now[1]", "test_buy_now_x1_live"]),
        ("BUY NGAY ×1 (Jupiter)", ["test_jupiter_buy_now[1]"]),
        ("SELL 25%/50%/100% theo số dư thực, PnL phân bổ giá vốn", ["test_manual_sell_partial_and_all_pnl"]),
        ("SELL qua Jupiter", ["test_jupiter_limit_x3_and_sell"]),
    ]),
    ("3. Bundle BUY ×3/×5", [
        ("BUY NGAY ×3/×5: giá tuần tự, tip ở tx cuối, sendBundle, xác minh từng tx",
         ["test_buy_now[3]", "test_buy_now[5]", "test_buy_now_x5_details"]),
        ("Bundle ×3/×5 qua Jupiter (quote lũy kế → minOut từng leg)", ["test_jupiter_buy_now[3]",
                                                                       "test_jupiter_buy_now[5]"]),
        ("Từ chối Bundle khi không có mô phỏng tuần tự (simulateBundle)",
         ["test_bundle_refused_without_sequential_simulation", "test_bundle_refused_when_rpc_lacks_simulate_bundle"]),
        ("Snapshot >600 ms trước khi gửi → chặn và chuẩn bị lại", ["test_snapshot_older_than_600ms_before_send_is_reprepared"]),
        ("Lỗi mô phỏng chặn gửi (×1 và ×3)", ["test_simulation_failure_blocks_send"]),
    ]),
    ("4. FIXED DIP BUY ×1/×3/×5", [
        ("Giá tham chiếu chốt khi ARM, trigger cố định, chạm là kích hoạt, giá thực hiện ≤ trần, chỉ 1 lần",
         ["test_dip_and_limit[1-DIP]", "test_dip_and_limit[3-DIP]", "test_dip_and_limit[5-DIP]"]),
        ("Không gửi khi phí+tác động giá làm leg vượt trần (chờ, vẫn ARMED)",
         ["test_dip_waits_when_legs_would_exceed_ceiling"]),
        ("Slippage 100% vẫn không vượt trần DIP (minOut/maxIn on-chain)", ["test_dip_with_100pct_slippage_never_above_ceiling"]),
        ("DIP ×5 qua gRPC realtime", ["test_geyser_stream_snapshots_heartbeat_and_dip"]),
    ]),
    ("5. LIMIT BUY ×1/×3/×5", [
        ("Giá LIMIT cố định, kích hoạt khi giá ≤ LIMIT, giá thực hiện ≤ LIMIT, chỉ 1 lần",
         ["test_dip_and_limit[1-LIMIT]", "test_dip_and_limit[3-LIMIT]", "test_dip_and_limit[5-LIMIT]"]),
        ("Front-run trước khi land: guard on-chain từ chối, FAILED_SAFE, không gửi lại",
         ["test_frontrun_cannot_break_price_ceiling"]),
        ("LIMIT ×3 qua Jupiter; Jupiter trả minOut yếu hơn → từ chối",
         ["test_jupiter_limit_x3_and_sell", "test_jupiter_weaker_encoded_min_is_refused_for_limit"]),
        ("Hủy lệnh chờ", ["test_cancel_armed_order", "test_limit_requires_price_then_arm_list_and_cancel"]),
    ]),
    ("6. Auto Sell TP/SL và PnL", [
        ("TP kích hoạt SELL 100%, PnL dương", ["test_auto_tp_sl[TP-1.6-TP]"]),
        ("SL kích hoạt SELL 100%, PnL âm", ["test_auto_tp_sl[SL-0.75-SL]"]),
        ("TP/SL chỉ bật sau khi BUY đã xác minh", ["test_tpsl_not_active_before_buy_verified"]),
        ("TP/SL gắn vào vị thế của cả DIP/LIMIT", ["test_dip_and_limit[5-DIP]", "test_dip_and_limit[5-LIMIT]"]),
    ]),
    ("7. Lỗi, mất kết nối, khởi động lại", [
        ("UNKNOWN/HOLD: không mua trùng, không tự xóa, đối soát 2 RPC → FAILED_SAFE khi blockhash hết hạn",
         ["test_unknown_hold_blocks_duplicates_then_reconciles"]),
        ("ACK ≠ SUCCESS; land muộn vẫn được xác minh", ["test_ack_is_not_success_and_late_landing"]),
        ("Jito 429: gửi lại đúng bytes đã ký, chỉ land 1 lần", ["test_jito_429_resends_identical_bytes_once_landed"]),
        ("Jito lỗi: không coi là thất bại cho tới khi blockhash hết hạn", ["test_jito_unreachable_is_not_assumed_failed_until_expiry"]),
        ("Khởi động lại: khôi phục DIP/LIMIT (giá chốt), vị thế, đối soát lệnh đang treo",
         ["test_restart_restores_armed_orders_and_pending_verification"]),
        ("Khởi động lại: không gửi lại lệnh PREPARED/TRIGGERED", ["test_restart_never_resends_prepared_or_triggered"]),
        ("Mất RPC lúc kích hoạt (chưa gửi): DIP về ARMED, không mất lệnh, chỉ gửi 1 lần",
         ["test_rpc_outage_at_trigger_rearms_dip_without_sending"]),
        ("Snapshot cũ không bao giờ kích hoạt", ["test_stale_snapshot_never_triggers"]),
        ("Lệnh LIVE không chạy khi bot ở NO_SEND", ["test_live_order_does_not_fire_while_bot_in_no_send"]),
        ("Hai lệnh giống hệt chạy đồng thời có chữ ký khác nhau", ["test_identical_concurrent_orders_get_distinct_signatures"]),
    ]),
    ("8. Tích hợp toàn bộ 9 hình thức BUY", [
        ("NO_SEND: cả 9 hình thức chuẩn bị + ký + mô phỏng, không gửi, không đổi số dư",
         ["test_no_send_all_nine_forms"]),
        ("Telegram end-to-end: BUY NGAY ×3, DIP ×5 + SELL + chi tiết bundle, LIMIT ×5 ARM/hủy",
         ["test_buy_now_x3_via_telegram_live", "test_dip_x5_via_telegram_then_sell_and_bundle_detail",
          "test_limit_requires_price_then_arm_list_and_cancel"]),
        ("Script nghiệm thu NO_SEND mainnet chạy đúng", ["test_nosend_check_script_all_pass"]),
    ]),
]


def main() -> None:
    (ROOT / "reports").mkdir(exist_ok=True)
    junit = ROOT / "reports/junit.xml"
    t0 = time.time()
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", f"--junitxml={junit}"], cwd=ROOT,
                          capture_output=True, text=True)
    dur = time.time() - t0
    tree = ET.parse(junit)
    results: dict[str, str] = {}
    for tc in tree.iter("testcase"):
        name = tc.get("name")
        status = "PASS"
        if tc.find("failure") is not None or tc.find("error") is not None:
            status = "FAIL"
        elif tc.find("skipped") is not None:
            status = "SKIP"
        results[name] = status
    total = len(results)
    passed = sum(1 for s in results.values() if s == "PASS")
    lines = ["# Báo cáo nghiệm thu (tự động)", "",
             f"Thời điểm: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}  ",
             f"Bộ test: **{passed}/{total} PASS** trong {dur:.1f} s (`python scripts/acceptance_report.py`)", "",
             "Môi trường kiểm thử: chuỗi Solana giả lập trong bộ nhớ (`tests/fakechain.py`) thực thi chính các "
             "giao dịch đã ký của bot (kiểm chữ ký, blockhash, thứ tự account, dữ liệu instruction, guard "
             "maxIn/minOut on-chain), RPC/Jito/Jupiter/Telegram giả lập qua HTTP, Yellowstone gRPC bằng server "
             "gRPC thật trong tiến trình. Toán PumpSwap đối chiếu golden của SDK chính thức và dữ liệu "
             "FeeConfig mainnet.", "",
             "> Chưa chạy trên mainnet từ môi trường này (không có mạng tới Solana/Jito/Telegram). Trước LIVE "
             "phải chạy `scripts/nosend_check.py` trên VPS — xem README.", ""]
    for phase, reqs in REQS:
        lines += [f"## {phase}", "", "| Yêu cầu | Kết quả | Bằng chứng (test) |", "|---|---|---|"]
        for desc, tests in reqs:
            st = [results.get(t, "MISSING") for t in tests]
            verdict = "PASS" if all(s == "PASS" for s in st) else "FAIL"
            ev = "<br>".join(f"`{t}` {s}" for t, s in zip(tests, st))
            lines.append(f"| {desc} | **{verdict}** | {ev} |")
        lines.append("")
    evp = ROOT / "reports/evidence.json"
    if evp.exists():
        ev = json.loads(evp.read_text())
        o, s = ev["order"], ev["submission"]
        lines += ["## Bằng chứng mẫu: FIXED DIP ×5 (Bundle)", "",
                  f"Lệnh #{o['id']} DIP {o['dip_pct']}% — giá tham chiếu `{o['reference_price']}` → giá kích hoạt/trần "
                  f"`{o['trigger_price']}` SOL/token — trạng thái **{o['status']}**  ",
                  f"Mô phỏng: `{s['sim_detail']}` — xác minh level {s['verify_level']} (2 RPC) — snapshot "
                  f"{s['snapshot_age_ms']:.1f} ms — bundle `{s['bundle_id']}`  ",
                  f"Mốc thời gian (ms từ tín hiệu): {ev['timings_ms']}", "",
                  "| Leg | Token dự kiến | Token thực nhận | maxIn (lamports) | SOL vào pool | Giá thực hiện | Trần | Tip | Bytes |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for l in ev["legs"]:
            lines.append(f"| {l['idx'] + 1} | {l['planned_out_raw']} | {l['actual_token_delta']} | {l['max_in_lamports']} "
                         f"| {l['swap_lamports']} | {float(l['exec_price']):.6e} | {float(l['limit_price']):.6e} "
                         f"| {l['tip_paid_lamports']} | {l['tx_bytes']} |")
        p = ev["position"]
        lines += ["", f"Vị thế: {p['token_raw']} token raw, giá vốn {p['cost_lamports']} lamports "
                      f"(rent tách riêng {p['rent_lamports']}), TP {p['tp_pct']}% / SL {p['sl_pct']}% bật={p['tpsl_enabled']}", ""]
    fails = [n for n, s in results.items() if s != "PASS"]
    lines += ["## Danh sách test", "", f"Tổng {total}, PASS {passed}, khác: {fails or 'không'}", ""]
    out = ROOT / "docs/ACCEPTANCE_REPORT.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print(proc.stdout[-500:])
    print(f"wrote {out}: {passed}/{total} PASS")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
