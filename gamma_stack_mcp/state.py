"""Tracks the single background `up` process.

Within one MCP-server process the live ``Popen`` handle is authoritative. We also
persist minimal metadata (pid, log path, args) to ``.run/up.json`` so that if the
MCP server restarts while a ``run.sh`` up is still churning through a cold pull, a
fresh ``stack_status`` can still report on it via PID liveness.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from . import runner


@dataclass
class UpRecord:
    pid: int
    log_path: str
    args: list[str]
    started_iso: Optional[str] = None
    exit_code: Optional[int] = None  # filled in once the owning process reaps it


_proc: Optional[subprocess.Popen] = None
_record: Optional[UpRecord] = None


def _meta_path() -> Path:
    return runner.state_dir() / "up.json"


def _persist(rec: Optional[UpRecord]) -> None:
    p = _meta_path()
    if rec is None:
        p.unlink(missing_ok=True)
        return
    p.write_text(json.dumps(asdict(rec), indent=2))


def _load_persisted() -> Optional[UpRecord]:
    p = _meta_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
        return UpRecord(**data)
    except (json.JSONDecodeError, TypeError, OSError):
        return None


def record_up(proc: subprocess.Popen, log_path: Path, args: list[str],
              started_iso: Optional[str]) -> UpRecord:
    global _proc, _record
    _proc = proc
    _record = UpRecord(pid=proc.pid, log_path=str(log_path), args=list(args),
                       started_iso=started_iso)
    _persist(_record)
    return _record


def is_up_running() -> bool:
    """True if a tracked up-process is still alive.

    Prefers the live Popen handle (knows exit codes); falls back to the persisted
    PID when the handle was lost to a server restart.
    """
    if _proc is not None:
        return _proc.poll() is None
    rec = _record or _load_persisted()
    return runner.pid_alive(rec.pid) if rec else False


def up_process_status() -> dict:
    """Structured view of the tracked up-process for stack_status."""
    global _record
    rec = _record or _load_persisted()
    if rec is None:
        return {"tracked": False}

    if _proc is not None:
        code = _proc.poll()
        running = code is None
        # Persist the exit code the moment we observe it, so a later call (even
        # after a server restart) can still report `failed` instead of `down`.
        if code is not None and rec.exit_code != code:
            rec.exit_code = code
            _persist(rec)
        return {
            "tracked": True,
            "running": running,
            "pid": rec.pid,
            "exit_code": code,
            "log_path": rec.log_path,
            "started": rec.started_iso,
            "args": rec.args,
        }

    # No live handle (server restarted): infer from PID liveness. The exit code is
    # known only if a prior call in the owning process reaped and persisted it.
    running = runner.pid_alive(rec.pid)
    return {
        "tracked": True,
        "running": running,
        "pid": rec.pid,
        "exit_code": None if running else rec.exit_code,
        "exit_code_note": (
            None if running or rec.exit_code is not None
            else "unknown (exit not observed by this server instance)"
        ),
        "log_path": rec.log_path,
        "started": rec.started_iso,
        "args": rec.args,
    }


def forget_up() -> None:
    """Unconditionally drop the tracked up-process record.

    Used by stack_down: tearing the stack down makes any in-flight up moot. A
    still-running run.sh (e.g. mid health-poll) is left detached — its output keeps
    flowing to up.log and it exits on its own; we simply stop tracking it.
    """
    global _proc, _record
    _proc = None
    _record = None
    _persist(None)


def current_record() -> Optional[UpRecord]:
    return _record or _load_persisted()


# ── last-up mode (default vs coexist) ─────────────────────────────────────────
def _mode_path() -> Path:
    return runner.state_dir() / "mode.json"


def record_mode(coexist: bool, offset: int, keep: list[str]) -> None:
    """Remember how the last stack_up was launched, so stack_setup can match it."""
    _mode_path().write_text(json.dumps(
        {"coexist": coexist, "offset": offset, "keep": sorted(keep)}, indent=2))


def read_mode() -> dict:
    p = _mode_path()
    if not p.is_file():
        return {"coexist": False, "offset": 0, "keep": []}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"coexist": False, "offset": 0, "keep": []}
