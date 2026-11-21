# Tự triển khai solbot lên VPS Tokyo

Gói `solbot-<phiên bản>.tar.gz` chứa toàn bộ mã nguồn, test, script cài đặt và tài liệu. Gói **không** chứa
`.env`, ví hay bất kỳ token nào; bạn điền trên VPS. Không cần Git trên VPS.

**Yêu cầu:**
- VPS ở Tokyo, chạy Ubuntu 22.04/24.04 hoặc Debian 12, có quyền `sudo`; khuyến nghị 2 vCPU, 2 GB RAM.
- Python ≥ 3.11. Trên Ubuntu 22.04, script tự cài Python 3.12.

---

## 1. Tải gói lên VPS

Trên máy Mac (thư mục chứa file tải về):

```bash
shasum -a 256 -c solbot-*.sha256          # phải in: OK
scp solbot-*.tar.gz <user>@<ip-vps>:~
ssh <user>@<ip-vps>
```

## 2. Giải nén và cài đặt (trên VPS)

```bash
tar xzf solbot-*.tar.gz        # tạo thư mục ~/solbot
cd ~/solbot
sudo bash deploy/install.sh
```

Script sẽ:
- tạo user hệ thống `solbot`;
- cài code vào `/opt/solbot`, tạo `.venv` và cài thư viện;
- tạo `/opt/solbot/.env` từ mẫu, đặt quyền 600/700;
- cài unit systemd và chạy toàn bộ test.

Dòng cuối phải là `83 passed`. Chạy lại script nhiều lần cũng an toàn: không ghi đè `.env`, `data/`, `secrets/`.

## 3. Điền cấu hình

```bash
sudo nano /opt/solbot/.env
```

| Biến | Giá trị |
|---|---|
| `RPC_URLS` | `https://mainnet.helius-rpc.com/?api-key=<KEY>,https://solana-rpc.publicnode.com` |
| `SIM_RPC_URL` | giống `RPC_URLS` (danh sách thử lần lượt khi gặp 429) |
| `BUNDLE_SIM_RPC_URL` | URL Helius (đã kiểm chứng hỗ trợ `simulateBundle`, cần cho ×3/×5) |
| `VERIFY_RPC_A_URL` / `VERIFY_RPC_B_URL` | URL Helius / `https://solana-rpc.publicnode.com` (2 nhà cung cấp khác nhau) |
| `GRPC_ENDPOINT` / `GRPC_TOKEN` | `solana-yellowstone-grpc.publicnode.com:443` / token cá nhân lấy ở allnodes.com/publicnode |
| `JITO_URL` | `https://tokyo.mainnet.block-engine.jito.wtf` |
| `TELEGRAM_BOT_TOKEN` | token lấy từ @BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | ID Telegram của bạn (lấy từ @userinfobot) |
| `WALLET_ENCRYPTED_PATH` | `secrets/wallet.enc.json` |
| `ALLOW_LIVE` | **giữ `false`** cho tới khi nghiệm thu xong |

## 4. Tạo ví (keystore mã hóa)

```bash
cd /opt/solbot
sudo -u solbot .venv/bin/python scripts/wallet_tool.py new --out secrets/wallet.enc.json
#   hoặc nhập ví có sẵn: ... wallet_tool.py import --keypair /đường/dẫn/id.json --out secrets/wallet.enc.json
```

Lệnh in ra địa chỉ ví (pubkey). Gửi một ít SOL vào ví này: mô phỏng ở NO_SEND dùng số dư thật nhưng không chi tiêu.

Cấp passphrase cho systemd (passphrase không nằm trong `.env`):

```bash
sudo mkdir -p /etc/solbot
read -rs -p "Passphrase: " P && echo -n "$P" | sudo systemd-creds encrypt --name=wallet_pass - /etc/solbot/wallet_pass.cred; unset P; echo
cd ~/solbot && sudo bash deploy/install.sh     # chạy lại để bật dòng LoadCredentialEncrypted
```

## 5. Kiểm tra trước khi chạy (không gửi giao dịch nào)

```bash
cd /opt/solbot
sudo -u solbot .venv/bin/python -m solbot doctor          # hỏi passphrase ví để kiểm tra giải mã + số dư
sudo -u solbot .venv/bin/python scripts/mainnet_probe.py
```

Kết quả mong đợi:
- `doctor`: **0 FAIL**. Telegram getMe PASS, gRPC nhận slot update, Jito RTT < 10 ms (ở Tokyo).
- `mainnet_probe.py`: **0 FAIL** và bảng thời gian tín hiệu → Jito ACK. Lưu lại file `reports/mainnet_probe.md`.

Nghiệm thu 9 hình thức BUY ở NO_SEND (cần ví đã có SOL):

```bash
sudo -u solbot .venv/bin/python scripts/nosend_check.py --mint <MINT_TOKEN_TRÊN_PUMPSWAP> --sol 0.01   # hỏi passphrase
```

## 6. Chạy bot

```bash
sudo systemctl enable --now solbot
journalctl -u solbot -f                 # theo dõi log; Ctrl+C để thoát xem log (bot vẫn chạy)
```

Trên Telegram, gửi `/menu` cho bot. Màn hình phải hiện ví, số dư và **Chế độ: NO_SEND**. Tiếp theo dán một mint token:
thẻ token phải có đủ 9 nút BUY (NGAY/DIP/LIMIT × 1/3/5), SELL, TP/SL.

**Checklist NO_SEND:**
- [ ] BUY NGAY ×1, ×3, ×5: báo "NO_SEND: đã chuẩn bị + mô phỏng, không gửi".
- [ ] ARM một lệnh DIP và một lệnh LIMIT, xem ở "Lệnh chờ", rồi hủy.
- [ ] `sudo systemctl restart solbot`: các lệnh ARMED vẫn còn, giá trigger không đổi.
- [ ] `/stats` có số đo thời gian.

## 7. Bật LIVE (chỉ khi mọi mục trên đạt)

1. Giảm giới hạn rủi ro trong Telegram → ⚙️ Cài đặt, ví dụ `max_sol_per_tx` = 0.02 và `max_daily_sol` = 0.1.
2. `sudo nano /opt/solbot/.env` → `ALLOW_LIVE=true`, rồi `sudo systemctl restart solbot`.
3. Telegram → `/menu` → nút **Chế độ** → gõ đúng cụm xác nhận `LIVE xxxx` (4 ký tự cuối địa chỉ ví).
4. Thử BUY NGAY ×1 với số SOL nhỏ trước. Xem chi tiết lệnh (chữ ký, slot, thời gian) và `/stats`.

Tắt LIVE bất cứ lúc nào: bấm lại **Chế độ** (về NO_SEND ngay), hoặc đặt `ALLOW_LIVE=false` rồi restart.

## 8. Cập nhật phiên bản mới

```bash
tar xzf solbot-<bản-mới>.tar.gz   # giải nén đè lên ~/solbot
cd ~/solbot && sudo bash deploy/install.sh && sudo systemctl restart solbot
```

`.env`, ví, database (lệnh ARMED, vị thế, HOLD) được giữ nguyên.

## 9. Sao lưu

Sao lưu định kỳ ra nơi an toàn:
- `/opt/solbot/secrets/wallet.enc.json` **và** passphrase (cất riêng);
- `/opt/solbot/data/solbot.sqlite3` (lệnh, vị thế, lịch sử).

Mất cả file ví lẫn passphrase thì không khôi phục được ví.

## 10. Xử lý lỗi thường gặp

| Hiện tượng | Cách xử lý |
|---|---|
| `install.sh`: không tìm thấy Python ≥ 3.11 | Cài Python 3.11+ rồi chạy `sudo PYTHON=/usr/bin/python3.12 bash deploy/install.sh` |
| `doctor`: gRPC `PERMISSION_DENIED` | Sai/thiếu `GRPC_TOKEN`. Bot vẫn chạy bằng HTTP polling (chậm hơn) |
| Nhiều HTTP 429 | RPC gói free bị giới hạn: thêm endpoint vào `RPC_URLS`/`SIM_RPC_URL` hoặc dùng gói trả phí |
| `simulateBundle` không hỗ trợ | Đặt `BUNDLE_SIM_RPC_URL` là Helius; nếu không, ×3/×5 bị từ chối ở LIVE (×1 vẫn chạy) |
| Service không khởi động, log báo thiếu passphrase | Kiểm tra `/etc/solbot/wallet_pass.cred` và đã chạy lại `install.sh` sau khi tạo credential |
| Telegram không trả lời | Kiểm tra `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` (đúng ID của bạn) và `doctor` |
| Lệnh ở trạng thái UNKNOWN/HOLD | Bot đang đối soát với 2 RPC và **không** gửi lại. Đợi, hoặc bấm "Kiểm tra lại" trong mục HOLD |

Tài liệu chi tiết: `README.md`, `docs/MAINNET_NO_SEND_REPORT.md`, `docs/ACCEPTANCE_REPORT.md`.
