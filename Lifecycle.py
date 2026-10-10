"""Persistent service diagnostics, separate from trading authorization and credentials."""

import json
import os
import re
import threading
from collections import deque
from pathlib import Path

_lock = threading.RLock()
_errors = {}
_FIELDS = {"pid", "currencies", "reason", "returnCode", "heartbeatAtMs", "authorized", "buildMismatch"}


def _path(context):
    return Path(context.state_db_path).resolve().parent / "lifecycle.jsonl"


def record(context, event, **details):
    """Only diagnostic scalar fields are accepted; never persist keys or API payloads."""
    path = _path(context)
    if not re.fullmatch(r"[A-Z0-9_]{1,64}", event):
        raise ValueError("Invalid lifecycle event")
    clean = {}
    for key in _FIELDS:
        if key not in details:
            continue
        value = details[key]
        if key == "currencies":
            value = [coin for coin in value if coin in {"USD", "USDT"}]
        elif key == "reason":
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_:.-]{1,128}", value):
                continue
        elif value is not None and not isinstance(value, (int, float, bool)):
            continue
        clean[key] = value
    value = {"atMs": int(context.now() * 1000), "event": event, **clean}
    with _lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(value, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            _errors.pop(str(path), None)
            return True
        except OSError as exc:
            _errors[str(path)] = type(exc).__name__
            return False


def status(context):
    path = _path(context)
    with _lock:
        try:
            with path.open(encoding="utf-8") as stream:
                lines = deque(stream, maxlen=32)
            events = [json.loads(line) for line in lines]
            if any(not isinstance(row, dict) or "event" not in row or "atMs" not in row for row in events):
                raise ValueError("Invalid lifecycle record")
            return {
                "available": True,
                "events": events,
                "latest": events[-1] if events else None,
                "recordingError": _errors.get(str(path)),
            }
        except FileNotFoundError:
            return {"available": False, "events": [], "latest": None, "recordingError": _errors.get(str(path))}
        except (OSError, ValueError) as exc:
            return {"available": False, "events": [], "latest": None, "recordingError": type(exc).__name__}
