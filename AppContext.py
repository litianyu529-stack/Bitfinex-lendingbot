import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ProcessState:
    process: Any = None
    started_at: str | None = None
    log_handle: Any = None
    stop_reason: str | None = None
    preflight: dict | None = None
    dashboard_server: Any = None
    market_hub: Any = None
    lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass(frozen=True)
class AppContext:
    project_root: str
    config_path: str
    status_path: str
    state_db_path: str
    live_lock_path: str
    dashboard_lock_path: str
    process_log_path: str
    client_factory: Callable[..., Any] | None = None
    now: Callable[[], float] = time.time
    process_state: ProcessState = field(default_factory=ProcessState)

    @classmethod
    def for_project(
        cls,
        project_root,
        config_path="default.cfg",
        status_path=os.path.join("www", "botlog.json"),
        state_db_path=os.path.join(".state", "lendingbot-v3.sqlite3"),
        client_factory=None,
        now=time.time,
        process_state=None,
    ):
        root = os.path.abspath(project_root)

        def rooted(path):
            return path if os.path.isabs(path) else os.path.join(root, path)

        return cls(
            project_root=root,
            config_path=rooted(config_path),
            status_path=rooted(status_path),
            state_db_path=rooted(state_db_path),
            live_lock_path=rooted(os.path.join(".state", "lendingbot-live.lock")),
            dashboard_lock_path=rooted(os.path.join(".state", "lendingbot-dashboard.lock")),
            process_log_path=rooted(os.path.join("www", "bot-process.log")),
            client_factory=client_factory,
            now=now,
            process_state=process_state or ProcessState(),
        )
