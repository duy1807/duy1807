#!/usr/bin/env bash
# One-command installer for the Tokyo VPS (Ubuntu/Debian). Idempotent: safe to re-run for updates.
#
#   sudo bash deploy/install.sh                      # from a checkout of the repo
#   sudo REPO_URL=https://github.com/<you>/<repo>.git BRANCH=claude/vigilant-noether-ra13cp bash install.sh
#
# Env overrides: APP_DIR (/opt/solbot), APP_USER (solbot), REPO_URL, BRANCH,
#                NO_SYSTEMD=1 (skip unit install), SKIP_USER=1 (do not create the system user)
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/solbot}
APP_USER=${APP_USER:-solbot}
BRANCH=${BRANCH:-}
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || true)
PY=${PYTHON:-python3}

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "Kiểm tra Python ≥ 3.11"
"$PY" - <<'PYEOF'
import sys
if sys.version_info < (3, 11):
    sys.exit(f"Cần Python >= 3.11, đang có {sys.version.split()[0]}")
print(sys.version.split()[0])
PYEOF
if ! "$PY" -c "import venv, ensurepip" 2>/dev/null; then
  if command -v apt-get >/dev/null; then apt-get update -y && apt-get install -y python3-venv git; fi
fi

if [[ -z "${SKIP_USER:-}" ]] && ! id "$APP_USER" >/dev/null 2>&1; then
  say "Tạo user hệ thống $APP_USER"
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
mkdir -p "$APP_DIR"

say "Cài mã nguồn vào $APP_DIR"
if [[ -n "${REPO_URL:-}" ]]; then
  if [[ -d "$APP_DIR/.git" ]]; then
    git -C "$APP_DIR" fetch --quiet origin ${BRANCH:+"$BRANCH"}
    git -C "$APP_DIR" checkout --quiet ${BRANCH:-main}
    git -C "$APP_DIR" pull --quiet --ff-only
  else
    git clone --quiet ${BRANCH:+--branch "$BRANCH"} "$REPO_URL" "$APP_DIR.tmp"
    cp -a "$APP_DIR.tmp/." "$APP_DIR/" && rm -rf "$APP_DIR.tmp"
  fi
elif [[ -n "$SRC_DIR" && -f "$SRC_DIR/solbot/__init__.py" && "$SRC_DIR" != "$APP_DIR" ]]; then
  # copy the checkout, never overwriting local state (.env, data, logs, secrets)
  tar -C "$SRC_DIR" --exclude=.env --exclude=data --exclude=logs --exclude=secrets --exclude=.venv \
      --exclude='__pycache__' --exclude=.pytest_cache -cf - . | tar -C "$APP_DIR" -xf -
fi
test -f "$APP_DIR/solbot/__init__.py" || { echo "Không tìm thấy mã nguồn trong $APP_DIR"; exit 1; }

say "Môi trường Python (.venv) + thư viện"
"$PY" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

say "Thư mục dữ liệu và quyền"
mkdir -p "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/secrets"
chmod 700 "$APP_DIR/secrets" "$APP_DIR/data"
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "Đã tạo $APP_DIR/.env từ mẫu — cần điền endpoint/Telegram/ví."
fi
chmod 600 "$APP_DIR/.env"
if [[ -z "${SKIP_USER:-}" ]]; then chown -R "$APP_USER:$APP_USER" "$APP_DIR"; fi

if [[ -z "${NO_SYSTEMD:-}" ]]; then
  say "Cài systemd unit"
  sed -e "s#/opt/solbot#$APP_DIR#g" -e "s#^User=solbot#User=$APP_USER#" \
      "$APP_DIR/deploy/solbot.service" > /etc/systemd/system/solbot.service
  if [[ ! -f /etc/solbot/wallet_pass.cred ]]; then
    # no encrypted passphrase credential yet: keep the unit startable for plain keypair files
    sed -i -e 's/^LoadCredentialEncrypted=/#LoadCredentialEncrypted=/' /etc/systemd/system/solbot.service
  fi
  systemctl daemon-reload
fi

say "Chạy bộ test (không cần mạng)"
(cd "$APP_DIR" && .venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -1)

cat <<NEXT

Xong. Các bước tiếp theo (theo thứ tự):
  1. Điền $APP_DIR/.env (RPC, BUNDLE_SIM_RPC_URL, VERIFY_RPC_A/B, GRPC_ENDPOINT+GRPC_TOKEN, TELEGRAM_*, giữ ALLOW_LIVE=false)
  2. Tạo ví mã hóa:   sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/scripts/wallet_tool.py new --out $APP_DIR/secrets/wallet.enc.json
     Passphrase cho systemd:  sudo mkdir -p /etc/solbot && echo -n 'PASS' | sudo systemd-creds encrypt --name=wallet_pass - /etc/solbot/wallet_pass.cred
     rồi chạy lại script này để bật dòng LoadCredentialEncrypted.
  3. Kiểm tra:        cd $APP_DIR && sudo -u $APP_USER .venv/bin/python -m solbot doctor
  4. NO_SEND mainnet: sudo -u $APP_USER .venv/bin/python scripts/mainnet_probe.py
                      sudo -u $APP_USER .venv/bin/python scripts/nosend_check.py --mint <MINT>
  5. Chạy bot:        sudo systemctl enable --now solbot && journalctl -u solbot -f
NEXT
