"""Entry point.

    python -m solbot            # run the bot (same as `run`)
    python -m solbot doctor     # pre-flight check of the deployment (nothing is sent)
"""
import argparse
import asyncio
import sys

from .app import run
from .config import Config
from .logging_setup import register_secret, setup_logging


def main() -> None:
    ap = argparse.ArgumentParser(prog="solbot")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "doctor"])
    args = ap.parse_args()
    config = Config.from_env()
    for s in (config.telegram_token, config.grpc_token, config.jupiter_api_key, config.jito_uuid):
        register_secret(s)
    if args.command == "doctor":
        from .doctor import Doctor

        setup_logging("ERROR")
        sys.exit(asyncio.run(Doctor(config).run()))
    setup_logging(config.log_level, config.log_dir)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
