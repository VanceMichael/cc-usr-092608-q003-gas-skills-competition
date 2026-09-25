"""只增不改的事件日志：断电重启后按原顺序重放，恢复现场状态。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class Journal:
    """追加式 JSONL 日志，每条事件带递增序号，写入即落盘。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._seq = 0
        if self.path.exists():
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        self._seq += 1

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """追加一条事件并强制落盘，返回带序号的事件。"""
        self._seq += 1
        event = {"seq": self._seq, "kind": kind, "payload": payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def events(self) -> list[dict[str, Any]]:
        """按写入顺序读出全部事件。"""
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
