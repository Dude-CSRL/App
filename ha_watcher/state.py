"""Device-ID dedup state stored as a JSON file, written atomically."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class SeenDevices:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._seen: set[str] = set()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._seen = set()
            return
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        self._seen = set(data.get("seen_device_ids", []))

    def __contains__(self, device_id: str) -> bool:
        return device_id in self._seen

    def __len__(self) -> int:
        return len(self._seen)

    def add(self, device_id: str) -> None:
        """Record a device ID and flush immediately (atomic replace)."""
        self._seen.add(device_id)
        self.flush()

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"seen_device_ids": sorted(self._seen)}
        fd, tmp = tempfile.mkstemp(prefix=".seen-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
