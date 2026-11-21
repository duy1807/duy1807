# solbot — Bot giao dịch Solana (PumpSwap · Jupiter · Jito) điều khiển qua Telegram

Bot viết mới hoàn toàn bằng Python (asyncio), chạy trên VPS Tokyo. Hỗ trợ **9 hình thức BUY**
(BUY NGAY / FIXED DIP / LIMIT × 1/3/5), SELL thủ công, Auto Sell TP/SL, Jito Bundle, xác minh on-chain
bằng 2 RPC độc lập, quản lý vị thế/PnL và khôi phục trạng thái sau khởi động lại.

**Mặc định luôn ở NO_SEND.** LIVE chỉ bật được khi operator đặt `ALLOW_LIVE=true` trên VPS *và* người dùng
gõ cụm xác nhận trong Telegram.

---

## 1. Kiến trúc

```
 Telegram ──► TelegramBot ──► OrderService ──┬─► TradeEngine (dùng chung cho cả 9 hình thức)
                                             │      plan legs tuần tự ─► RiskManager ─► build+sign
 Yellowstone gRPC ─► GeyserSource ─┐         │      ─► kiểm tra snapshot ≤600ms ─► Simulator
 HTTP RPC (poll dự phòng) ─────────┼► MarketEngine ─► snapshot ─► TriggerManager (DIP/LIMIT)
 Jupiter quote ────────────────────┘                          └► AutoSell (TP/SL)
                                                    ─► lưu SENT ─► JitoClient (Tokyo) ─► ACK
                                                    ─► Verifier (RPC A + RPC B) ─► Positions/PnL
 SQLite (WAL) ◄── mọi trạng thái: lệnh, bundle, giao dịch con, receipt, vị thế, HOLD, log, metrics
 Recovery: khôi phục khi khởi động + vòng đối soát HOLD định kỳ
```

| Module | File |
|---|---|
| Telegram Controller | `solbot/telegram_bot.py` |
| Wallet Manager | `solbot/wallet.py`, `scripts/wallet_tool.py` |
| Market Data Engine | `solbot/market.py`, `solbot/geyser.py` |
| PumpSwap Engine | `solbot/dex/pumpswap.py` |
| Jupiter Engine | `solbot/dex/jupiter.py` |
| BUY/SELL + Bundle ×3/×5 Engine | `solbot/engine.py` (một pipeline duy nhất) |
| FIXED DIP / LIMIT Engine | `solbot/triggers.py` (chỉ là điều kiện kích hoạt) |
| Simulation Engine | `solbot/simulation.py` |
| Jito Transport | `solbot/jito.py` |
| Transaction Verification | `solbot/verify.py` |
| Auto Sell TP/SL | `solbot/autosell.py` |
| Position & PnL | `solbot/positions.py` |
| Database & Recovery | `solbot/db.py`, `solbot/recovery.py` |
| Risk Manager | `solbot/risk.py` |
| Cấu hình | `solbot/config.py` (env), `solbot/settings.py` (tham số chỉnh qua Telegram) |

BUY NGAY, DIP, LIMIT **dùng chung** `TradeEngine.execute_buy()`; khác nhau duy nhất ở thời điểm gọi và việc
có áp trần giá hay không.

## 2. Cơ chế an toàn chính

**Trần giá DIP/LIMIT không thể bị slippage vô hiệu hóa.** PumpSwap `buy_v2` là exact-out
(`base_amount_out`, `max_quote_amount_in`). Với mỗi leg i (tính trên trạng thái pool *sau* các leg 0..i-1):

```
maxIn_i = min( SOL × (1 + slippage) ,  floor(base_out_i × giá_trần) )
```

Chương trình on-chain từ chối mọi lần thực thi có chi phí (kể cả phí pool) > `maxIn_i`, nên giá thực hiện của
**từng** giao dịch trong Bundle ≤ giá trần — kể cả slippage 100% hoặc bị front-run. Nếu kế hoạch dự kiến của bất
kỳ leg nào vượt trần (do phí + tác động giá), bot **không gửi** và lệnh vẫn ARMED (ghi chú lý do).
Với Jupiter: `minOut_i ≥ ceil(SOL / giá_trần)`, bot giải mã instruction đã build để kiểm tra minOut thực sự
được mã hóa; không kiểm được thì từ chối (DIP/LIMIT).

* Giá kích hoạt = giá spot ≤ giá trigger, trên snapshot ≤ 600 ms. Giá DIP: `tham_chiếu × (1 − DIP%/100)`,
  tham chiếu chốt khi ARM; không trailing, không chờ nến, không tự sửa.
* Snapshot được kiểm tra lại sau khi ký và sau khi mô phỏng; quá 600 ms → bỏ kế hoạch, chuẩn bị lại bằng dữ liệu mới.
* ×1 mô phỏng bằng `simulateTransaction`; ×3/×5 bằng `simulateBundle` (thực thi **tuần tự**). Không có
  `simulateBundle` → từ chối gửi Bundle ở LIVE.
* Trạng thái `SENT` + chữ ký được ghi DB **trước** khi gửi Jito → crash lúc nào cũng không mất dấu.
* ACK ≠ SUCCESS. SUCCESS chỉ khi `getTransaction` có `meta.err = null`; số token/SOL thực tế tính từ
  pre/post balances; RPC thứ 2 đối chiếu (verify level 2). FAILED_SAFE chỉ khi land với lỗi, hoặc **cả 2 RPC**
  báo block height đã vượt `lastValidBlockHeight` mà không thấy chữ ký.
* UNKNOWN/HOLD: không gửi lại, chặn BUY mới cùng token, không có nút xóa ép; vòng đối soát chạy liên tục
  (kể cả sau restart).
* Mỗi lệnh DIP/LIMIT chỉ kích hoạt 1 lần (compare-and-set `ARMED → TRIGGERED`); sau khi đã gửi không bao giờ ARM lại.
* Không fallback gửi qua RPC: lớp RPC không có hàm `sendTransaction`. Jito lỗi 429/kết nối → chỉ gửi lại
  **đúng bytes đã ký** (cùng chữ ký, không thể thực thi 2 lần).
* Mỗi giao dịch có nonce CU nhỏ → hai lệnh giống hệt cùng blockhash không bao giờ trùng chữ ký.
* TP/SL chỉ bật sau khi BUY đã được xác minh và token thực nhận đã ghi vào vị thế.

## 3. Cài đặt trên VPS Tokyo

Cách nhanh (Ubuntu/Debian, chạy lại được để cập nhật):

```bash
git clone <repo> && cd <repo> && git checkout claude/vigilant-noether-ra13cp
sudo bash deploy/install.sh          # user solbot, /opt/solbot, .venv, systemd unit, chạy test
sudo nano /opt/solbot/.env           # điền endpoint, GRPC_TOKEN, Telegram; giữ ALLOW_LIVE=false
cd /opt/solbot && sudo -u solbot .venv/bin/python -m solbot doctor
```

Hoặc từng bước thủ công:

```bash
sudo useradd -r -m -d /opt/solbot solbot && sudo -iu solbot
git clone <repo> /opt/solbot && cd /opt/solbot
python3.12 -m venv .venv && . .venv/bin/activate     # Python ≥ 3.11
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env && nano .env   # điền endpoint, Telegram, ALLOW_LIVE=false
mkdir -p secrets data logs && chmod 700 secrets
python scripts/wallet_tool.py new --out secrets/wallet.enc.json      # hoặc: import --keypair ~/id.json
```

Passphrase keystore được nạp qua systemd credential (không nằm trong `.env`):

```bash
sudo mkdir -p /etc/solbot
echo -n 'PASSPHRASE' | sudo systemd-creds encrypt --name=wallet_pass - /etc/solbot/wallet_pass.cred
sudo cp deploy/solbot.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now solbot && journalctl -u solbot -f
```

Endpoint cần có (xem `.env.example`):

| Biến | Vai trò |
|---|---|
| `RPC_URLS` | đọc tài khoản/số dư/blockhash (nhiều endpoint = failover) |
| `SIM_RPC_URL` | `simulateTransaction` — danh sách phân cách dấu phẩy, tự chuyển khi 429/timeout/node chậm slot |
| `BUNDLE_SIM_RPC_URL` | `simulateBundle` (RPC Jito-Solana) — **bắt buộc cho ×3/×5 LIVE** |
| `VERIFY_RPC_A_URL`, `VERIFY_RPC_B_URL` | 2 nhà cung cấp **khác nhau** để xác minh/đối soát |
| `GRPC_ENDPOINT`, `GRPC_TOKEN` | Yellowstone gRPC (PublicNode cần token cá nhân). Thiếu/hỏng → tự chuyển HTTP polling 300 ms |
| `JITO_URL` | `https://tokyo.mainnet.block-engine.jito.wtf` |

## 4. Nghiệm thu NO_SEND trên mainnet (bắt buộc trước LIVE)

```bash
. .venv/bin/activate
python scripts/nosend_check.py --mint <MINT_TOKEN_ĐÃ_LÊN_PUMPSWAP> --sol 0.01
```

Script ép NO_SEND, dùng DB tạm, **không gửi gì**; kiểm tra: từng RPC (slot/độ trễ/lệch slot), 2 RPC xác minh,
hỗ trợ `simulateBundle`, Jito `getTipAccounts`, luồng gRPC (tỷ lệ snapshot ≤ 600 ms), Jupiter; giải mã pool,
bậc phí, giá/MC/thanh khoản; rồi lập kế hoạch + ký + **mô phỏng trên trạng thái mainnet thật** BUY NGAY ×1/×3/×5,
LIMIT ×1/×3/×5 và ARM DIP ×1/×3/×5. Kết quả: `reports/nosend_check.md` (PASS/FAIL từng mục).
Ví cần có đủ SOL để mô phỏng thành công (mô phỏng dùng số dư thật nhưng không chi tiêu).

Chỉ khi mọi mục PASS mới cân nhắc đặt `ALLOW_LIVE=true`, khởi động lại, rồi trong Telegram bấm
**Chế độ** và gõ `LIVE xxxx` (4 ký tự cuối địa chỉ ví). Nên bắt đầu với `max_sol_per_tx`/`max_daily_sol` nhỏ.

### Đã chạy thử trên mainnet

Xem [`docs/MAINNET_NO_SEND_REPORT.md`](docs/MAINNET_NO_SEND_REPORT.md). Đã kiểm chứng trên pool PumpSwap thật bằng
mô phỏng (không ký, không gửi): `buy_v2`/`sell_v2` và Bundle tuần tự ×3 khớp chương trình tới từng lamport
(chi phí bot tính → chấp nhận, −1 lamport → từ chối). Thời gian tín hiệu → Jito ACK: p50 229–299 ms cho cả 6 loại
×1/×3/×5, đo từ máy không ở Tokyo. Kiểm tra lại bất cứ lúc nào:

```bash
python scripts/mainnet_probe.py            # tự tìm pool đang hoạt động; báo cáo: reports/mainnet_probe.md
```

## 5. Sử dụng Telegram

* `/menu` — ví, số dư, chế độ, số lệnh chờ/vị thế/HOLD; nút Lệnh chờ, Vị thế, Lịch sử, HOLD, Cài đặt, Hiệu năng,
  Chế độ, Tạm dừng.
* Dán **địa chỉ mint** → thẻ token: giá, Market Cap, thanh khoản, pool, phí pool, số dư SOL/token, giá vốn, PnL,
  TP/SL; hàng nút `BUY NGAY ×1/×3/×5`, `DIP BUY ×1/×3/×5`, `LIMIT BUY ×1/×3/×5`, `SELL 25/50/75/100/%…`,
  và chỉnh SOL/Slippage/TP/SL/DIP/LIMIT cho lệnh này.
* Mọi lệnh có màn hình xác nhận (token một lần, hết hạn 120 s).
* Lệnh chờ: giá tham chiếu, giá kích hoạt, giá hiện tại (% cách trigger), trạng thái, ghi chú vì sao chưa kích hoạt,
  nút Hủy, nút chi tiết (submission, bundle id, blockhash, từng giao dịch con, mốc thời gian ms).
* Vị thế: giá vốn, giá trị theo quote bán thực, PnL %, PnL đã chốt; SELL 50/100%, bật/tắt TP/SL, sửa TP, sửa SL.
* `/stats` — p50/p95: tín hiệu→mô phỏng, mô phỏng, Jito ACK, gửi→landed.

## 6. Định nghĩa số liệu

* **Giá** = SOL / token (đơn vị UI). Giá spot = `effective_quote / base_reserve`
  (`effective_quote = quote_vault + Pool.virtual_quote_reserves`, theo tài liệu PumpSwap).
* **Giá thực hiện** = SOL vào pool (đã gồm mọi phí pool) / token thực nhận — dùng để so với trần DIP/LIMIT.
* **Giá vốn** = toàn bộ SOL rời ví cho lệnh mua (pool + phí mạng + priority + tip), *không* gồm rent có thể thu hồi
  (ATA, user_volume_accumulator — theo dõi riêng).
* **PnL thực hiện** = SOL thực nhận khi bán (sau phí mạng + tip) − giá vốn phân bổ (bình quân) cho lượng đã bán.
* **TP/SL** = (giá trị bán toàn bộ vị thế theo quote chính xác hiện tại − giá vốn) / giá vốn.

## 7. Database & khôi phục

SQLite WAL (`data/solbot.sqlite3`): `settings, wallets, tokens, pools, orders, submissions (bundle),
sub_txs (giao dịch con + receipt), positions, fills, sells, events (log lỗi), metrics`.
Khi khởi động: submission `PREPARED` (chưa từng gửi) → FAILED_SAFE; `SENT/ACK/PENDING/LANDED/UNKNOWN_HOLD` →
tiếp tục xác minh; lệnh `TRIGGERED` chưa có submission → FAILED_SAFE (không gửi lại); DIP/LIMIT `ARMED` được
khôi phục với đúng giá tham chiếu/giá trigger đã chốt; vị thế mở tiếp tục theo dõi TP/SL.

## 8. Kiểm thử

```bash
pip install -r requirements.txt
python -m pytest -q                       # 83 test
python scripts/acceptance_report.py       # chạy tất cả + ghi docs/ACCEPTANCE_REPORT.md (PASS/FAIL theo yêu cầu)
```

`tests/fakechain.py` là chuỗi giả lập thực thi chính bytes giao dịch đã ký của bot (kiểm chữ ký, blockhash,
thứ tự 17 account, PDA, dữ liệu instruction, guard maxIn/minOut, Bundle nguyên tử, phí, rent) và phục vụ
JSON-RPC (kể cả `simulateBundle`), Jito, Jupiter, Telegram qua HTTP giả lập; gRPC kiểm thử bằng server gRPC thật
trong tiến trình. Toán PumpSwap được đối chiếu golden của `@pump-fun/pump-swap-sdk` 2.1.0 và dữ liệu FeeConfig
mainnet.

## 9. Giới hạn đã biết — cần xác nhận trên VPS

* Bộ code được xây và kiểm thử **không có kết nối mainnet** (môi trường phát triển không truy cập được
  Solana/Jito/Telegram). Layout PumpSwap lấy từ IDL/tài liệu chính thức (`buy_v2`/`sell_v2`, 17 account);
  phải chạy `scripts/nosend_check.py` trên mainnet và xem mô phỏng PASS trước LIVE.
* PumpSwap: pool quote WSOL, hỗ trợ v2. Pool "cashback coin" (không hỗ trợ v2) và token còn trên bonding curve
  được đi qua Jupiter.
* Jupiter: giá theo dõi bằng quote định kỳ (không phải gRPC); kiểm tra minOut chỉ giải mã được instruction v6
  `route`/`shared_accounts_route` — dạng khác thì DIP/LIMIT qua Jupiter bị từ chối (an toàn), BUY NGAY vẫn dựa
  vào slippage của Jupiter.
* Trần giá tính **gồm phí pool**, nên với pool phí ~1.25% giá spot phải xuống dưới trigger khoảng phí + tác động
  giá trước khi lệnh được gửi (đúng tinh thần "không mua cao hơn giá đã khóa").
* Chế độ một ví (theo giai đoạn đầu của đặc tả).
