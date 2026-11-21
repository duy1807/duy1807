"""Entry point: python -m solbot"""
import asyncio

from .app import run
from .config import Config
from .logging_setup import register_secret, setup_logging


def main() -> None:
    config = Config.from_env()
    register_secret(config.telegram_token)
    register_secret(config.grpc_token)
    register_secret(config.jupiter_api_key)
    register_secret(config.jito_uuid)
    setup_logging(config.log_level, config.log_dir)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
