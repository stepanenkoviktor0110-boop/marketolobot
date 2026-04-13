"""Persistent task storage for web UI jobs."""

import json
from pathlib import Path
from typing import Dict, Optional

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
TASKS_FILE = DATA_DIR / "tasks.json"


def _ensure_file():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not TASKS_FILE.exists():
        TASKS_FILE.write_text("{}", encoding="utf-8")


def load_tasks() -> Dict[str, dict]:
    _ensure_file()
    try:
        return json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def save_tasks(tasks: Dict[str, dict]):
    _ensure_file()
    TASKS_FILE.write_text(json.dumps(tasks, indent=2, ensure_ascii=False), encoding="utf-8")


def get_task(job_id: str) -> Optional[dict]:
    tasks = load_tasks()
    return tasks.get(job_id)


def update_task(job_id: str, **kwargs):
    tasks = load_tasks()
    if job_id in tasks:
        tasks[job_id].update(kwargs)
        save_tasks(tasks)


def cleanup_tasks(max_size: int = 150):
    tasks = load_tasks()
    if len(tasks) > max_size:
        items = list(tasks.items())
        tasks = dict(items[-max_size:])
        save_tasks(tasks)
