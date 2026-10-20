from __future__ import annotations

import asyncio
import logging
import sys

from .agent import HostAgent
from .config import HostAgentConfig


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    cfg = HostAgentConfig.from_env()
    agent = HostAgent(cfg)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
