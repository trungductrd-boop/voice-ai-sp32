"""Validation, parsing, and queueing for open-loop robot movement commands."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from threading import Lock

MAX_DURATION_MS = 10_000
DEFAULT_DURATION_MS = 1_000
DEFAULT_SPEED = 160
VALID_ACTIONS = frozenset({"forward", "backward", "left", "right", "stop"})


@dataclass(frozen=True)
class RobotCommand:
    action: str
    speed: int
    duration_ms: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "action": self.action,
            "speed": self.speed,
            "duration_ms": self.duration_ms,
        }


class RobotCommandError(ValueError):
    """Raised when a command doesn't satisfy the robot protocol."""


class RobotCommandQueue:
    def __init__(self) -> None:
        self._commands: deque[RobotCommand] = deque()
        self._lock = Lock()

    def put(self, command: RobotCommand) -> None:
        with self._lock:
            self._commands.append(command)

    def get(self) -> RobotCommand | None:
        with self._lock:
            return self._commands.popleft() if self._commands else None


def validate_command(action: str, speed: int, duration_ms: int) -> RobotCommand:
    if action not in VALID_ACTIONS:
        raise RobotCommandError("action must be one of: forward, backward, left, right, stop")
    if not isinstance(speed, int) or isinstance(speed, bool) or not 0 <= speed <= 255:
        raise RobotCommandError("speed must be an integer from 0 to 255")
    if (
        not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or not 0 <= duration_ms <= MAX_DURATION_MS
    ):
        raise RobotCommandError("duration_ms must be an integer from 0 to 10000")
    if action == "stop":
        return RobotCommand(action, 0, 0)
    if duration_ms == 0:
        raise RobotCommandError("duration_ms must be greater than 0 for movement")
    return RobotCommand(action, speed, duration_ms)


def command_from_dict(data: dict) -> RobotCommand:
    if not isinstance(data, dict):
        raise RobotCommandError("request body must be a JSON object")
    try:
        return validate_command(data["action"], data.get("speed", DEFAULT_SPEED), data["duration_ms"])
    except KeyError as exc:
        raise RobotCommandError(f"missing field: {exc.args[0]}") from exc


def parse_robot_command(text: str) -> RobotCommand | None:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    if not normalized:
        return None
    if re.search(r"\b(stop|dừng|dung)\b", normalized):
        return validate_command("stop", 0, 0)

    action = None
    patterns = {
        "forward": r"(go|move)\s+forward|đi\s+(tới|thẳng|về phía trước)|di\s+(toi|thang|ve phia truoc)",
        "backward": r"(go|move)\s+backward|đi\s+lùi|di\s+lui",
        "left": r"turn\s+left|quay\s+trái|quay\s+trai",
        "right": r"turn\s+right|quay\s+phải|quay\s+phai",
    }
    for candidate, pattern in patterns.items():
        if re.search(pattern, normalized):
            action = candidate
            break
    if action is None:
        return None

    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(milliseconds?|ms|seconds?|secs?|s|giây|giay)", normalized)
    duration_ms = DEFAULT_DURATION_MS
    if match:
        amount = float(match.group(1).replace(",", "."))
        unit = match.group(2)
        duration_ms = round(amount if unit.startswith(("ms", "millisecond")) else amount * 1000)
    duration_ms = min(duration_ms, MAX_DURATION_MS)
    return validate_command(action, DEFAULT_SPEED, duration_ms)


def confirmation_for(command: RobotCommand) -> str:
    if command.action == "stop":
        return "Đã dừng robot."
    names = {
        "forward": "đi thẳng",
        "backward": "đi lùi",
        "left": "quay trái",
        "right": "quay phải",
    }
    return f"Được, tôi sẽ {names[command.action]}."
