# solbot — hướng dẫn cho Claude Code (đọc trước khi làm bất cứ việc gì)

Bot giao dịch Solana mới (Python asyncio): BUY NGAY / FIXED DIP / LIMIT ×1/×3/×5, SELL, TP/SL, PumpSwap + Jupiter,
Jito Bundle, Telegram. Nhánh làm việc: `claude/vigilant-noether-ra13cp`. Tài liệu: `README.md`,
`docs/MAINNET_NO_SEND_REPORT.md`, `docs/ACCEPTANCE_REPORT.md`.

## Quy tắc an toàn (bắt buộc)

1. **Không bật LIVE.** Không đặt `ALLOW_LIVE=true`, không chuyển chế độ LIVE trong Telegram/DB, trừ khi người dùng
   nói rõ ràng trong chính phiên làm việc này rằng họ phê duyệt giao dịch thật. Mặc định luôn NO_SEND.
2. **Không đọc to, in ra, commit hay gửi đi bí mật:** `.env`, file ví (`secrets/`, `*.enc.json`, `id.json`),
   passphrase, token Telegram, GRPC_TOKEN, API key RPC. Khi cần, người dùng tự gõ vào file/terminal của họ.
   Không `cat .env`; kiểm tra cấu hình bằng `python -m solbot doctor` (đã che bí mật).
3. **Không dùng bot cũ:** không đọc/sao chép code, DB hay module từ các thư mục `ALOBOT_*` hay bot cũ nào khác.
4. **Không sửa/khởi tạo lại khóa ví** khi người dùng chưa yêu cầu. Không xóa `data/` (chứa lệnh ARMED, vị thế,
   trạng thái UNKNOWN/HOLD) và không xóa trạng thái HOLD để "ép" giao dịch.
5. Trước khi commit: `python -m pytest -q` phải PASS; không commit `.env`, `data/`, `secrets/`, `logs/`.

## Chạy thử trên máy local (macOS/Linux)

```bash
python3.12 -m venv .venv && . .venv/bin/activate     # cần Python >= 3.11 (macOS: brew install python@3.12)
pip install -r requirements.txt
python -m pytest -q                                  # toàn bộ test phải PASS
cp -n .env.example .env && chmod 600 .env            # NGƯỜI DÙNG tự điền giá trị (RPC, GRPC_TOKEN, TELEGRAM_*)
python -m solbot doctor                              # kiểm tra cấu hình + endpoint (không gửi gì)
python scripts/mainnet_probe.py                      # kiểm chứng trên mainnet bằng mô phỏng (không ký, không gửi)
```

## Triển khai lên VPS Tokyo (Ubuntu/Debian) qua SSH

Hỏi người dùng địa chỉ VPS và user SSH (ví dụ `ssh ubuntu@<ip>`), không hỏi mật khẩu/khóa.

```bash
# trên VPS
git clone https://github.com/duy1807/duy1807.git ~/solbot-src && cd ~/solbot-src
git checkout claude/vigilant-noether-ra13cp
sudo bash deploy/install.sh          # user solbot, /opt/solbot, .venv, systemd unit, chạy test
```

Sau đó **người dùng tự** (không làm thay, không xem giá trị):
- `sudo nano /opt/solbot/.env`: điền RPC_URLS, SIM_RPC_URL, BUNDLE_SIM_RPC_URL, VERIFY_RPC_A_URL/B (2 nhà cung cấp
  khác nhau), GRPC_ENDPOINT + GRPC_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS, giữ `ALLOW_LIVE=false`.
- Tạo ví: `sudo -u solbot /opt/solbot/.venv/bin/python /opt/solbot/scripts/wallet_tool.py new --out /opt/solbot/secrets/wallet.enc.json`
- Passphrase cho systemd: `echo -n '<passphrase>' | sudo systemd-creds encrypt --name=wallet_pass - /etc/solbot/wallet_pass.cred`
  (cần `sudo mkdir -p /etc/solbot` trước), rồi chạy lại `sudo bash deploy/install.sh` để bật credential.

Rồi kiểm tra và chạy:

```bash
cd /opt/solbot
sudo -u solbot .venv/bin/python -m solbot doctor                       # mục tiêu: 0 FAIL (Telegram, gRPC PASS)
sudo -u solbot .venv/bin/python scripts/mainnet_probe.py               # mục tiêu: 0 FAIL, E2E p50 < 500 ms
sudo -u solbot .venv/bin/python scripts/nosend_check.py --mint <MINT>  # 9 hình thức BUY ở NO_SEND
sudo systemctl enable --now solbot && journalctl -u solbot -f
```

## Checklist nghiệm thu NO_SEND trên VPS (báo cáo lại cho người dùng)

- [ ] `doctor`: 0 FAIL; Jito RTT < 10 ms; gRPC nhận slot update; Telegram getMe OK
- [ ] `mainnet_probe.py`: 0 FAIL; ghi lại bảng E2E (p50/max) cho BUY NGAY và DIP/LIMIT ×1/×3/×5
- [ ] `nosend_check.py`: BUY NGAY ×1/×3/×5 và LIMIT ×1/×3/×5 = DRY_RUN với mô phỏng OK (ví cần có SOL); DIP ARM đúng
- [ ] Telegram: `/menu` trả lời; dán mint → thẻ token có đủ 9 nút BUY; ARM một DIP ở NO_SEND rồi hủy
- [ ] `systemctl restart solbot` → lệnh ARMED vẫn còn với cùng giá trigger
- [ ] Gửi kết quả (đã che token/địa chỉ RPC có key) cho người dùng; KHÔNG bật LIVE

## Gợi ý xử lý lỗi thường gặp

- `doctor` gRPC `PERMISSION_DENIED`: thiếu/sai `GRPC_TOKEN` (PublicNode cần token cá nhân); bot vẫn chạy bằng HTTP polling.
- HTTP 429 nhiều: RPC gói free bị giới hạn; thêm endpoint vào `RPC_URLS`/`SIM_RPC_URL` (failover) hoặc dùng gói trả phí.
- `simulateBundle` không hỗ trợ: đặt `BUNDLE_SIM_RPC_URL` là RPC hỗ trợ (Helius đã kiểm chứng hỗ trợ); nếu không, ×3/×5 bị từ chối ở LIVE.
- Thiếu passphrase khi chạy service: kiểm tra `/etc/solbot/wallet_pass.cred` và dòng `LoadCredentialEncrypted` trong unit.
