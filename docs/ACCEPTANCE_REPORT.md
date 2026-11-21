# Báo cáo nghiệm thu (tự động)

Thời điểm: 2026-11-21 23:31:52 UTC  
Bộ test: **88/88 PASS** trong 14.4 s (`python scripts/acceptance_report.py`)

Môi trường kiểm thử: chuỗi Solana giả lập trong bộ nhớ (`tests/fakechain.py`) thực thi chính các giao dịch đã ký của bot (kiểm chữ ký, blockhash, thứ tự account, dữ liệu instruction, guard maxIn/minOut on-chain), RPC/Jito/Jupiter/Telegram giả lập qua HTTP, Yellowstone gRPC bằng server gRPC thật trong tiến trình. Toán PumpSwap đối chiếu golden của SDK chính thức và dữ liệu FeeConfig mainnet.

> Chưa chạy trên mainnet từ môi trường này (không có mạng tới Solana/Jito/Telegram). Trước LIVE phải chạy `scripts/nosend_check.py` trên VPS — xem README.

## 1. Hạ tầng, Telegram, ví, database

| Yêu cầu | Kết quả | Bằng chứng (test) |
|---|---|---|
| Ví: private key không lộ (file 600, keystore mã hóa, che log/Telegram) | **PASS** | `test_wallet_file_permissions_enforced` PASS<br>`test_encrypted_keystore_roundtrip` PASS<br>`test_log_redaction` PASS<br>`test_no_secret_ever_sent` PASS |
| RPC: HTTP 429, timeout, failover, stale slot | **PASS** | `test_rpc_429_failover` PASS<br>`test_stale_rpc_node_is_skipped` PASS |
| RPC không bao giờ gửi giao dịch (không fallback public RPC) | **PASS** | `test_rpc_never_exposes_send_transaction` PASS |
| Polling gộp: nhiều token = 1 getMultipleAccounts/nhịp; tải nền chia đều các provider | **PASS** | `test_polling_is_batched_across_markets` PASS<br>`test_background_reads_are_spread_over_providers` PASS |
| Mô phỏng: 429 → chuyển endpoint; mọi endpoint 429 → DIP về ARMED, không bị đốt | **PASS** | `test_simulation_rate_limit_fails_over_and_never_burns_dip` PASS |
| Mô phỏng không bao giờ chạy trên bank cũ hơn snapshot (minContextSlot, -32016 → node khác) | **PASS** | `test_simulation_never_runs_on_a_bank_older_than_the_snapshot` PASS |
| gRPC bị từ chối (thiếu/sai token) → tự chuyển HTTP polling, DIP/LIMIT vẫn hoạt động | **PASS** | `test_grpc_rejected_falls_back_to_http_polling` PASS |
| Yellowstone gRPC: subscribe, heartbeat ≤600 ms, gom theo chữ ký, mất kết nối/resync | **PASS** | `test_geyser_stream_snapshots_heartbeat_and_dip` PASS<br>`test_geyser_disconnect_marks_stale_and_resyncs` PASS |
| PumpSwap: discriminator/IDL, thứ tự 17 account, PDA, toán phí khớp SDK chính thức (golden) | **PASS** | `test_instruction_discriminators_match_idl_and_anchor_hash` PASS<br>`test_v2_account_order_matches_idl` PASS<br>`test_known_pdas` PASS<br>`test_decode_mainnet_fee_config` PASS<br>`test_buy_base_input_golden_flat_fees` PASS<br>`test_sell_base_input_golden_flat_fees` PASS<br>`test_sell_mainnet_fee_tier_goldens` PASS<br>`test_buy_quote_roundtrip_never_exceeds_budget` PASS<br>`test_sequential_legs_raise_price` PASS<br>`test_pool_decode_roundtrip_and_short_layout` PASS |
| Cài đặt: NO_SEND mặc định, kiểm tra miền giá trị + giới hạn cứng | **PASS** | `test_settings_defaults_and_validation` PASS<br>`test_settings_validation_and_stale_confirm` PASS |
| LIVE chỉ bật khi ALLOW_LIVE=true + người dùng gõ xác nhận | **PASS** | `test_live_requires_operator_flag_and_confirmation` PASS<br>`test_live_toggle_requires_phrase` PASS<br>`test_live_locked_without_operator_flag` PASS |
| Telegram: chỉ user được phép; giao diện token đủ 9 nút BUY + SELL + tham số | **PASS** | `test_unauthorized_user_is_ignored` PASS<br>`test_token_card_has_all_nine_buy_buttons_and_sell` PASS |

## 2. BUY ×1 và SELL

| Yêu cầu | Kết quả | Bằng chứng (test) |
|---|---|---|
| BUY NGAY ×1 (PumpSwap) gửi Jito sendTransaction, xác minh on-chain, tạo vị thế | **PASS** | `test_buy_now[1]` PASS<br>`test_buy_now_x1_live` PASS |
| BUY NGAY ×1 (Jupiter) | **PASS** | `test_jupiter_buy_now[1]` PASS |
| SELL 25%/50%/100% theo số dư thực, PnL phân bổ giá vốn | **PASS** | `test_manual_sell_partial_and_all_pnl` PASS |
| SELL qua Jupiter | **PASS** | `test_jupiter_limit_x3_and_sell` PASS |

## 3. Bundle BUY ×3/×5

| Yêu cầu | Kết quả | Bằng chứng (test) |
|---|---|---|
| BUY NGAY ×3/×5: giá tuần tự, tip ở tx cuối, sendBundle, xác minh từng tx | **PASS** | `test_buy_now[3]` PASS<br>`test_buy_now[5]` PASS<br>`test_buy_now_x5_details` PASS |
| Bundle ×3/×5 qua Jupiter (quote lũy kế → minOut từng leg) | **PASS** | `test_jupiter_buy_now[3]` PASS<br>`test_jupiter_buy_now[5]` PASS |
| Từ chối Bundle khi không có mô phỏng tuần tự (simulateBundle) | **PASS** | `test_bundle_refused_without_sequential_simulation` PASS<br>`test_bundle_refused_when_rpc_lacks_simulate_bundle` PASS |
| Snapshot >600 ms trước khi gửi → chặn và chuẩn bị lại | **PASS** | `test_snapshot_older_than_600ms_before_send_is_reprepared` PASS |
| Lỗi mô phỏng chặn gửi (×1 và ×3) | **PASS** | `test_simulation_failure_blocks_send` PASS |

## 4. FIXED DIP BUY ×1/×3/×5

| Yêu cầu | Kết quả | Bằng chứng (test) |
|---|---|---|
| Giá tham chiếu chốt khi ARM, trigger cố định, chạm là kích hoạt, giá thực hiện ≤ trần, chỉ 1 lần | **PASS** | `test_dip_and_limit[1-DIP]` PASS<br>`test_dip_and_limit[3-DIP]` PASS<br>`test_dip_and_limit[5-DIP]` PASS |
| Không gửi khi phí+tác động giá làm leg vượt trần (chờ, vẫn ARMED) | **PASS** | `test_dip_waits_when_legs_would_exceed_ceiling` PASS |
| Slippage 100% vẫn không vượt trần DIP (minOut/maxIn on-chain) | **PASS** | `test_dip_with_100pct_slippage_never_above_ceiling` PASS |
| DIP ×5 qua gRPC realtime | **PASS** | `test_geyser_stream_snapshots_heartbeat_and_dip` PASS |

## 5. LIMIT BUY ×1/×3/×5

| Yêu cầu | Kết quả | Bằng chứng (test) |
|---|---|---|
| Giá LIMIT cố định, kích hoạt khi giá ≤ LIMIT, giá thực hiện ≤ LIMIT, chỉ 1 lần | **PASS** | `test_dip_and_limit[1-LIMIT]` PASS<br>`test_dip_and_limit[3-LIMIT]` PASS<br>`test_dip_and_limit[5-LIMIT]` PASS |
| Front-run trước khi land: guard on-chain từ chối, FAILED_SAFE, không gửi lại | **PASS** | `test_frontrun_cannot_break_price_ceiling` PASS |
| LIMIT ×3 qua Jupiter; Jupiter trả minOut yếu hơn → từ chối | **PASS** | `test_jupiter_limit_x3_and_sell` PASS<br>`test_jupiter_weaker_encoded_min_is_refused_for_limit` PASS |
| Hủy lệnh chờ | **PASS** | `test_cancel_armed_order` PASS<br>`test_limit_requires_price_then_arm_list_and_cancel` PASS |

## 6. Auto Sell TP/SL và PnL

| Yêu cầu | Kết quả | Bằng chứng (test) |
|---|---|---|
| TP kích hoạt SELL 100%, PnL dương | **PASS** | `test_auto_tp_sl[TP-1.6-TP]` PASS |
| SL kích hoạt SELL 100%, PnL âm | **PASS** | `test_auto_tp_sl[SL-0.75-SL]` PASS |
| TP/SL chỉ bật sau khi BUY đã xác minh | **PASS** | `test_tpsl_not_active_before_buy_verified` PASS |
| TP/SL gắn vào vị thế của cả DIP/LIMIT | **PASS** | `test_dip_and_limit[5-DIP]` PASS<br>`test_dip_and_limit[5-LIMIT]` PASS |

## 7. Lỗi, mất kết nối, khởi động lại

| Yêu cầu | Kết quả | Bằng chứng (test) |
|---|---|---|
| UNKNOWN/HOLD: không mua trùng, không tự xóa, đối soát 2 RPC → FAILED_SAFE khi blockhash hết hạn | **PASS** | `test_unknown_hold_blocks_duplicates_then_reconciles` PASS |
| ACK ≠ SUCCESS; land muộn vẫn được xác minh | **PASS** | `test_ack_is_not_success_and_late_landing` PASS |
| Jito 429: gửi lại đúng bytes đã ký, chỉ land 1 lần | **PASS** | `test_jito_429_resends_identical_bytes_once_landed` PASS |
| Jito lỗi: không coi là thất bại cho tới khi blockhash hết hạn | **PASS** | `test_jito_unreachable_is_not_assumed_failed_until_expiry` PASS |
| Khởi động lại: khôi phục DIP/LIMIT (giá chốt), vị thế, đối soát lệnh đang treo | **PASS** | `test_restart_restores_armed_orders_and_pending_verification` PASS |
| Khởi động lại: không gửi lại lệnh PREPARED/TRIGGERED | **PASS** | `test_restart_never_resends_prepared_or_triggered` PASS |
| Mất RPC lúc kích hoạt (chưa gửi): DIP về ARMED, không mất lệnh, chỉ gửi 1 lần | **PASS** | `test_rpc_outage_at_trigger_rearms_dip_without_sending` PASS |
| Snapshot cũ không bao giờ kích hoạt | **PASS** | `test_stale_snapshot_never_triggers` PASS |
| Lệnh LIVE không chạy khi bot ở NO_SEND | **PASS** | `test_live_order_does_not_fire_while_bot_in_no_send` PASS |
| Hai lệnh giống hệt chạy đồng thời có chữ ký khác nhau | **PASS** | `test_identical_concurrent_orders_get_distinct_signatures` PASS |

## Triển khai (VPS)

| Yêu cầu | Kết quả | Bằng chứng (test) |
|---|---|---|
| systemd: passphrase ví qua credential (%d, $$), chạy được khi không có credential, hardening | **PASS** | `test_unit_passes_passphrase_from_credential` PASS<br>`test_unit_runs_without_credential` PASS<br>`test_unit_hardening_and_installer_present` PASS |
| Trình cài đặt hỏi–đáp: .env 600 + ALLOW_LIVE=false, ví mã hóa (tạo mới/nhập), tự lấy Telegram ID, bỏ giá trị mẫu, không in bí mật, bot đọc đúng cấu hình | **PASS** | `test_wizard_end_to_end_new_wallet` PASS<br>`test_generated_env_is_read_by_the_bot` PASS<br>`test_wizard_imports_existing_keypair` PASS<br>`test_rpc_url_from_key_or_url` PASS<br>`test_enable_credential_in_unit` PASS |

## 8. Tích hợp toàn bộ 9 hình thức BUY

| Yêu cầu | Kết quả | Bằng chứng (test) |
|---|---|---|
| NO_SEND: cả 9 hình thức chuẩn bị + ký + mô phỏng, không gửi, không đổi số dư | **PASS** | `test_no_send_all_nine_forms` PASS |
| Telegram end-to-end: BUY NGAY ×3, DIP ×5 + SELL + chi tiết bundle, LIMIT ×5 ARM/hủy | **PASS** | `test_buy_now_x3_via_telegram_live` PASS<br>`test_dip_x5_via_telegram_then_sell_and_bundle_detail` PASS<br>`test_limit_requires_price_then_arm_list_and_cancel` PASS |
| Script nghiệm thu NO_SEND mainnet chạy đúng | **PASS** | `test_nosend_check_script_all_pass` PASS |

## Bằng chứng mẫu: FIXED DIP ×5 (Bundle)

Lệnh #1 DIP 10% — giá tham chiếu `4.25E-7` → giá kích hoạt/trần `3.825E-7` SOL/token — trạng thái **FILLED**  
Mô phỏng: `simulateBundle OK units=[106450, 89450, 89450, 89450, 89600]` — xác minh level 2 (2 RPC) — snapshot 10.1 ms — bundle `c3b8712151184d92881b2bb7431f5f2e`  
Mốc thời gian (ms từ tín hiệu): {'t_planned': 1.63, 't_signed': 4.57, 't_simulated': 9.52, 't_sent': 9.99, 't_ack': 14.7, 't_landed': 19.13, 't_verified': 19.23}

| Leg | Token dự kiến | Token thực nhận | maxIn (lamports) | SOL vào pool | Giá thực hiện | Trần | Tip | Bytes |
|---|---|---|---|---|---|---|---|---|
| 1 | 282902419232 | 282902419232 | 108210175 | 99999999 | 3.534788e-07 | 3.825000e-07 | 0 | 795 |
| 2 | 282178383942 | 282178383942 | 107933231 | 99999999 | 3.543858e-07 | 3.825000e-07 | 0 | 795 |
| 3 | 281457124740 | 281457124740 | 107657350 | 99999999 | 3.552939e-07 | 3.825000e-07 | 0 | 795 |
| 4 | 280738627450 | 280738627450 | 107382524 | 99999999 | 3.562032e-07 | 3.825000e-07 | 0 | 795 |
| 5 | 280022877990 | 280022877990 | 107108750 | 99999999 | 3.571137e-07 | 3.825000e-07 | 100000 | 843 |

Vị thế: 1407299433354 token raw, giá vốn 500225812 lamports (rent tách riêng 3539280), TP 20% / SL 10% bật=1

## Danh sách test

Tổng 88, PASS 88, khác: không

