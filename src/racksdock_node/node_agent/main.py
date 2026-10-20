from __future__ import annotations

import asyncio
import logging
import sys

from .agent import NodeAgent
from .config import NodeAgentConfig


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    cfg = NodeAgentConfig.from_env()
    agent = NodeAgent(cfg)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
