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
PY=${PYTHON:-}

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
py_ok() { command -v "$1" >/dev/null 2>&1 && "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; }

say "Tìm Python ≥ 3.11"
if [[ -z "$PY" ]]; then
  for cand in python3.13 python3.12 python3.11 python3; do
    if py_ok "$cand"; then PY=$cand; break; fi
  done
fi
if [[ -z "$PY" ]] && command -v apt-get >/dev/null 2>&1; then
  # Ubuntu 22.04 ships Python 3.10: install 3.12 (deadsnakes PPA); Debian 12 already has 3.11
  apt-get update -y
  if grep -qi '^ID=ubuntu' /etc/os-release; then
    apt-get install -y software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -y
    apt-get install -y python3.12 python3.12-venv
    PY=python3.12
  else
    apt-get install -y python3 python3-venv
    py_ok python3 && PY=python3
  fi
fi
if [[ -z "$PY" ]] || ! py_ok "$PY"; then
  echo "Không tìm thấy Python >= 3.11. Cài Python 3.11+ rồi chạy lại (hoặc đặt PYTHON=/đường/dẫn/python3.12)."
  exit 1
fi
"$PY" --version
if ! "$PY" -c "import venv, ensurepip" 2>/dev/null; then
  if command -v apt-get >/dev/null 2>&1; then
    ver=$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
    apt-get install -y "python${ver}-venv" || apt-get install -y python3-venv
  fi
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

Xong. Bước tiếp theo (1 lệnh, hỏi–đáp: RPC, gRPC token, Telegram, ví, passphrase; rồi chạy doctor):
  sudo $APP_DIR/.venv/bin/python $APP_DIR/scripts/setup_wizard.py

Sau đó:
  Kiểm chứng mainnet (không gửi):  cd $APP_DIR && sudo -u $APP_USER .venv/bin/python scripts/mainnet_probe.py
  Log bot:                        journalctl -u solbot -f
  Hướng dẫn đầy đủ:               $APP_DIR/DEPLOY_VPS.md
NEXT
