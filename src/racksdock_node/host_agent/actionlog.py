"""Signed, append-only action log (docs/05 §3.3). Each entry is a SignedEnvelope streamed to the platform as a log event."""
from __future__ import annotations

from pathlib import Path

from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.keys import SigningKey


class ActionLog:
    def __init__(self, path: Path, key: SigningKey):
        self.path = path
        self.key = key
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev = self._last_hash()

    def _last_hash(self) -> str | None:
        try:
            lines = self.path.read_text().strip().splitlines()
            return SignedEnvelope.model_validate_json(lines[-1]).content_hash if lines else None
        except (OSError, IndexError, ValueError):
            return None

    def record(self, action: str, **fields) -> SignedEnvelope:
        env = SignedEnvelope.create("host_action", {"action": action, "prev": self._prev, **fields}, self.key)
        with self.path.open("a") as f:
            f.write(env.model_dump_json() + "\n")
        self._prev = env.content_hash
        return env
