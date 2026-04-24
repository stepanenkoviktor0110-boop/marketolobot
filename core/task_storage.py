"""Persistent task storage for web UI jobs.

All mutating operations use ``fcntl`` advisory file locks so two uvicorn
workers cannot lose each other's writes via the classic R-M-W race
(worker A reads {X}, worker B reads {}, both save their view).
"""

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, Optional

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
TASKS_FILE = DATA_DIR / "tasks.json"


def _ensure_file():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not TASKS_FILE.exists():
        TASKS_FILE.write_text("{}", encoding="utf-8")


@contextmanager
def _exclusive_tasks():
    """Open tasks.json under LOCK_EX and yield (tasks, save_fn).

    save_fn rewrites the file in-place while the lock is still held,
    so concurrent processes observe either the pre- or post-mutation
    state, never a partial write.
    """
    _ensure_file()
    with TASKS_FILE.open("r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            raw = f.read()
            tasks = json.loads(raw) if raw.strip() else {}

            def _save():
                f.seek(0)
                f.truncate()
                json.dump(tasks, f, ensure_ascii=False, indent=2)
                f.flush()

            yield tasks, _save
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_tasks() -> Dict[str, dict]:
    _ensure_file()
    try:
        with TASKS_FILE.open("r", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                return json.loads(f.read() or "{}")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def save_tasks(tasks: Dict[str, dict]):
    """Overwrite the whole file under LOCK_EX. Use only for bulk replace
    (e.g. cleanup). Prefer ``atomic_update_task`` / ``set_task`` / ``update_task``
    for targeted mutations."""
    _ensure_file()
    with TASKS_FILE.open("w", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def atomic_update_task(mutator: Callable[[Dict[str, dict]], None]):
    """Load → apply mutator in place → save, all under exclusive lock."""
    with _exclusive_tasks() as (tasks, save):
        mutator(tasks)
        save()


def set_task(job_id: str, task_data: dict):
    atomic_update_task(lambda tasks: tasks.__setitem__(job_id, task_data))


def update_task(job_id: str, **kwargs):
    def _do(tasks):
        if job_id in tasks:
            tasks[job_id].update(kwargs)
    atomic_update_task(_do)


def delete_task(job_id: str) -> bool:
    existed = {"value": False}

    def _do(tasks):
        if job_id in tasks:
            del tasks[job_id]
            existed["value"] = True

    atomic_update_task(_do)
    return existed["value"]


def get_task(job_id: str) -> Optional[dict]:
    return load_tasks().get(job_id)


def cleanup_tasks(max_size: int = 150):
    def _do(tasks):
        if len(tasks) > max_size:
            items = list(tasks.items())
            trimmed = dict(items[-max_size:])
            tasks.clear()
            tasks.update(trimmed)

    atomic_update_task(_do)
