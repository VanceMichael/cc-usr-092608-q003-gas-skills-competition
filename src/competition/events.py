"""现场竞赛运行服务的事件与事件日志。

所有现场处置（报名冻结、替补、更正、排程、录分、复核、设备故障、
裁定、申诉、并列处理）都先追加到事件日志，再应用到内存状态。
日志只可追加：任何更正都是新事件，最初材料永远留在日志里。
终评现场断电重启后，按原顺序重放事件即可恢复签到、封存赛题、
设备停用与待审申诉。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    payload: dict[str, Any]
    at: str

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "payload": self.payload, "at": self.at}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            seq=int(data["seq"]),
            type=str(data["type"]),
            payload=dict(data["payload"]),
            at=str(data["at"]),
        )


class EventStore:
    """只可追加的 JSONL 事件日志。"""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._events: list[Event] = []
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._events.append(Event.from_dict(json.loads(line)))
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event_type: str, payload: dict[str, Any], at: str) -> Event:
        event = Event(seq=len(self._events) + 1, type=event_type, payload=payload, at=at)
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._events.append(event)
        return event

    def load(self) -> list[Event]:
        """按原始顺序返回全部事件，供重启后重放。"""
        return list(self._events)
