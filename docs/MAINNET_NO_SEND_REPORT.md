# Báo cáo chạy thử trên Solana mainnet (NO_SEND)

Ngày chạy: 2026-11-21 (UTC). Số liệu chi tiết: [`reports/mainnet_probe.md`](../reports/mainnet_probe.md).
**Không có giao dịch nào được ký bằng ví thật hay được gửi đi.** Chạy lại trên VPS bằng
`python scripts/mainnet_probe.py` (tự tìm pool) hoặc `--mint <MINT>`.

## Endpoint đã tích hợp

| Vai trò | Endpoint | Kết quả |
|---|---|---|
| Đọc dữ liệu (RPC_URLS) | Helius (chính), PublicNode (dự phòng) | OK, RTT ấm ≈ 41–50 ms từ máy chạy thử |
| simulateTransaction (SIM_RPC_URL) | Helius → PublicNode (failover) | OK ≈ 50 ms |
| simulateBundle (BUNDLE_SIM_RPC_URL) | Helius | **Hỗ trợ** → Bundle ×3/×5 dùng được ở LIVE |
| Xác minh A / B | Helius / PublicNode (2 nhà cung cấp độc lập) | OK |
| Jito | `tokyo.mainnet.block-engine.jito.wtf` (chỉ `getTipAccounts`) | OK, RTT ≈ 178 ms *từ máy chạy thử (không ở Tokyo)* |
| Yellowstone gRPC | `solana-yellowstone-grpc.publicnode.com:443` | Cần **token cá nhân** (PublicNode trả `PERMISSION_DENIED` khi thiếu). Token đã lưu cho môi trường cloud, nhưng proxy của sandbox chỉ chèn header qua HTTP/1.1 nên gRPC (HTTP/2) không chạy được *trong sandbox*. Trên VPS đặt `GRPC_TOKEN` là chạy. Bot tự chuyển sang HTTP polling khi gRPC không khỏe. |

## Kiểm chứng tính đúng đắn trên trạng thái mainnet thật

Bot dựng giao dịch bằng chính code sản xuất (`TradeEngine.pumpswap_buy_ixs` / `pumpswap_sell_ixs`), mô phỏng với
`sigVerify=false` và một địa chỉ công khai có SOL làm fee payer giả lập (không ký, không gửi), trên pool PumpSwap
đang giao dịch (Token-2022, pool có `virtual_quote_reserves` > 0, nhiều bậc phí khác nhau qua các lần chạy).

| Kiểm tra | Kết quả |
|---|---|
| buy_v2 với `max_quote_amount_in` = chi phí bot tính → chương trình chấp nhận, nhận **đúng** `base_amount_out` | PASS |
| Cùng giao dịch với chi phí **− 1 lamport** → chương trình từ chối (`6004 ExceededSlippage`) | PASS |
| 3 leg tuần tự, mỗi leg `max_in` = chi phí planner tính trên trạng thái sau leg trước → chấp nhận; leg 3 − 1 lamport → từ chối | PASS |
| `simulateBundle` 3 giao dịch tuần tự | PASS |
| sell_v2 với `min_quote_amount_out` = số SOL bot tính → chấp nhận; + 1 lamport → từ chối | PASS |
| Trần DIP/LIMIT thấp hơn giá dự kiến 0,1% → planner từ chối, không dựng giao dịch | PASS |
| Slippage 100% + trần giá → `max_in` bị trần giới hạn (≈ 0,01 SOL thay vì 0,02 SOL) và vẫn khớp trên chuỗi | PASS |

Kết luận: layout 17 account của `buy_v2`/`sell_v2`, PDA, bậc phí theo market cap, `virtual_quote_reserves`
và mô hình trạng thái tuần tự của Bundle **khớp chương trình PumpSwap trên mainnet tới từng lamport**.
(Các lần chạy trước cũng PASS trên pool phí 1,25%, pool vốn hóa 2,8 triệu SOL và pool `virtual_quote_reserves` âm.)

## Tốc độ: tín hiệu → Jito ACK (yêu cầu < 0,5 s)

Đo đúng trình tự engine trên kết nối đã ấm (8 lần mỗi loại, p50):

| Hình thức | p50 | Dữ liệu | Plan+ký | Mô phỏng | Jito |
|---|---|---|---|---|---|
| BUY NGAY ×1 | **287 ms** | 56 ms | 0,4 ms | 49 ms | 182 ms |
| BUY NGAY ×3 | **294 ms** | 55 ms | 0,7 ms | 53 ms (bundle) | 180 ms |
| BUY NGAY ×5 | **299 ms** | 55 ms | 1,0 ms | 56 ms (bundle) | 179 ms |
| DIP/LIMIT ×1 | **229 ms** | 0 (stream + cache) | 0,3 ms | 50 ms | 179 ms |
| DIP/LIMIT ×3 | **231 ms** | 0 | 0,7 ms | 52 ms (bundle) | 179 ms |
| DIP/LIMIT ×5 | **241 ms** | 0 | 1,0 ms | 54 ms (bundle) | 180 ms |

* Tất cả p50 **< 500 ms**, kể cả khi đo từ máy không ở Tokyo. Khoảng 180 ms là RTT từ máy chạy thử tới Jito Tokyo;
  trên VPS Tokyo khoản này còn vài ms, nên tổng dự kiến chủ yếu là 1–2 RTT tới RPC (≈ 50–110 ms với độ trễ RPC đo được ở đây).
* Đỉnh 0,6–1,2 s xuất hiện vài lần: Jito mở lại kết nối TLS (≈ 3 RTT) và Helius gói free trả HTTP 429.
  Bot xử lý 429 bằng cách chuyển ngay sang endpoint mô phỏng khác.
* **Chưa đo được thời gian land trên chuỗi** vì cần gửi LIVE bằng ví có SOL. Bundle Jito chỉ land vào slot của
  leader chạy Jito (mỗi slot 400 ms), nên thời gian *xác nhận on-chain* < 0,5 s không thể bảo đảm trong mọi lần;
  phần bot kiểm soát (tín hiệu → gửi) đã < 0,5 s.

## Thay đổi code rút ra từ lần chạy mainnet

1. **Giữ kết nối ấm**: keepalive 90 s (httpx mặc định 5 s) + ping mọi endpoint mỗi 10 s. Kết nối lạnh đo được 0,4–1,4 s.
2. **Song song hóa**: snapshot và trạng thái ví lấy cùng lúc (BUY NGAY bớt 1 RTT); DIP/LIMIT dùng trạng thái ví đã
   prefetch trong lúc ARMED (bớt thêm 1 RTT). Mô phỏng vẫn luôn kiểm số dư thật.
3. **Mô phỏng nhiều endpoint**: `SIM_RPC_URL` nhận danh sách; 429/timeout chuyển endpoint ngay; nếu tất cả lỗi thì
   lệnh DIP/LIMIT quay về ARMED (lỗi hạ tầng không "đốt" lệnh một lần).
4. **`minContextSlot`**: mô phỏng không bao giờ chạy trên bank cũ hơn snapshot đã lập kế hoạch. Helius có node chậm
   slot (`-32016`), gặp thì chuyển node khác.
5. **gRPC → HTTP polling tự động** khi stream bị từ chối/đứt/im lặng quá `GRPC_STALE_MS` (1,5 s).
6. Mainnet đã có giao dịch **version 1**; giao dịch của bot vẫn là v0, `getTransaction` nhận tham số phiên bản.

## Khuyến nghị trước LIVE

* Đặt `GRPC_TOKEN` (PublicNode) trên VPS và chạy lại `scripts/nosend_check.py --mint <MINT>` để xác nhận luồng gRPC.
* Gói Helius free đã trả 429 ngay cả ở tải thử nghiệm, nên với giao dịch thật nên dùng gói trả phí hoặc node riêng.
* API key Helius đã được gửi qua chat; nên tạo key mới sau giai đoạn thử nghiệm. Key chỉ nằm trong `.env` cục bộ
  (đã gitignore), không có trong repo.
* Lần LIVE đầu: ví nhỏ, `max_sol_per_tx` nhỏ, ×1 trước, đo thời gian land thực tế (`/stats` trong Telegram).
