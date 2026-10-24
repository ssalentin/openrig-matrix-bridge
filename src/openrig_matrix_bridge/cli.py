from __future__ import annotations

import argparse
import asyncio
import logging
import os

from .bridge import MatrixBridge
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenRig Matrix bridge")
    parser.add_argument("--config", required=True, help="Path to config YAML")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("nio").setLevel(logging.INFO if args.verbose else logging.WARNING)
    token = os.environ.get("MATRIX_ACCESS_TOKEN", "")
    config = load_config(args.config)
    asyncio.run(MatrixBridge(config, token).run())

