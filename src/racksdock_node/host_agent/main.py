from __future__ import annotations

import asyncio
import logging
import sys

from .agent import HostAgent
from .config import HostAgentConfig


def main() -> int:
    if "--show-key" in sys.argv:
        # the host's service public key (hex): what `racksdock-miner bootstrap --host-pubkey` needs; created on first use
        from racksdock_subnet.core.keys import SigningKey

        print(SigningKey.load_or_create(HostAgentConfig.from_env().key_path).public.hex)
        return 0
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
