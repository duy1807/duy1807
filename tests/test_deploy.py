"""Deployment artefacts: systemd unit shell snippet and installer sanity."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _exec_start() -> str:
    for line in (ROOT / "deploy/solbot.service").read_text().splitlines():
        if line.startswith("ExecStart="):
            return line[len("ExecStart="):]
    raise AssertionError("no ExecStart")


def _systemd_expand(cmd: str, cred_dir: str) -> list[str]:
    # systemd: %d -> credentials directory, $$ -> literal $ ; then argv = /bin/sh -c '<script>'
    cmd = cmd.replace("%d", cred_dir).replace("$$", "$")
    prefix = "/bin/sh -c '"
    assert cmd.startswith(prefix) and cmd.endswith("'")
    return ["/bin/sh", "-c", cmd[len(prefix):-1]]


def test_unit_passes_passphrase_from_credential(tmp_path):
    (tmp_path / "wallet_pass").write_text("s3cret-passphrase")
    argv = _systemd_expand(_exec_start(), str(tmp_path))
    argv[2] = argv[2].replace("exec /opt/solbot/.venv/bin/python -m solbot run",
                              'printf "%s" "$WALLET_PASSPHRASE"')
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout
    assert out == "s3cret-passphrase"


def test_unit_runs_without_credential(tmp_path):
    argv = _systemd_expand(_exec_start(), str(tmp_path / "missing"))
    argv[2] = argv[2].replace("exec /opt/solbot/.venv/bin/python -m solbot run",
                              'printf "[%s]" "${WALLET_PASSPHRASE:-}"')
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout
    assert out == "[]"


def test_unit_hardening_and_installer_present():
    unit = (ROOT / "deploy/solbot.service").read_text()
    for k in ("NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=true", "EnvironmentFile=/opt/solbot/.env",
              "Restart=on-failure"):
        assert k in unit
    inst = (ROOT / "deploy/install.sh").read_text()
    assert "set -euo pipefail" in inst and "--exclude=.env" in inst and "chmod 600" in inst
    out = subprocess.run(["bash", "-n", str(ROOT / "deploy/install.sh")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
