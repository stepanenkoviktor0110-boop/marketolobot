"""State management and file storage for PMF projects."""

import json
import os
from datetime import datetime

from core.group_utils import (
    append_group_message,
    get_linked_project,
    get_user_display_name,
    link_group,
)

# Stage detection: check files from latest to earliest
STAGE_ARTIFACTS = [
    ("10_iterate", ["iteration-changelog.md"]),
    ("9_metrics", ["metrics-dashboard.md"]),
    ("8_mvp_launch", ["narrative-v3.md", "interview-synthesis.md"]),  # waiting stage
    ("7_interview_synthesis", ["interview-synthesis.md", "narrative-v3.md"]),
    ("6_field", ["interview-guide.md"]),  # waiting stage — user does interviews
    ("5_interview_prep", ["interview-guide.md"]),
    ("4_validation", ["assumptions-map.md"]),
    ("3_synthesis", ["risk-prioritization.md", "narrative-v2.md"]),
    ("2_research", ["market-research.md"]),
    ("1_hypothesis", ["narrative-v1.md"]),
    ("0_setup", ["00_setup.md"]),
]

STAGE_ORDER = [
    "0_setup", "1_hypothesis", "2_research", "3_synthesis",
    "4_validation", "5_interview_prep", "6_field",
    "7_interview_synthesis", "8_mvp_launch", "9_metrics", "10_iterate",
]

STAGE_NAMES_RU = {
    "0_setup": "Настройка",
    "1_hypothesis": "Гипотеза (7 измерений)",
    "2_research": "Исследование рынка",
    "3_synthesis": "Синтез и риски",
    "4_validation": "Валидация (DVF)",
    "5_interview_prep": "Подготовка к интервью",
    "6_field": "Полевые интервью",
    "7_interview_synthesis": "Синтез интервью",
    "8_mvp_launch": "Запуск MVP",
    "9_metrics": "Метрики",
    "10_iterate": "Итерация",
}


def get_project_path(projects_root: str, project_name: str) -> str:
    return os.path.join(projects_root, project_name)


def list_projects(projects_root: str) -> list[str]:
    if not os.path.exists(projects_root):
        return []
    return [
        d for d in sorted(os.listdir(projects_root))
        if os.path.isdir(os.path.join(projects_root, d))
    ]


def create_project(projects_root: str, project_name: str) -> str:
    path = get_project_path(projects_root, project_name)
    os.makedirs(path, exist_ok=True)
    os.makedirs(os.path.join(path, "output"), exist_ok=True)
    os.makedirs(os.path.join(path, "interviews", "notes"), exist_ok=True)
    os.makedirs(os.path.join(path, "inbox"), exist_ok=True)
    state = {
        "project_name": project_name,
        "created_at": datetime.now().isoformat(),
        "current_stage": "0_setup",
        "last_active": datetime.now().isoformat(),
        "tokens_used": 0,
        "conversation": [],  # stores Q&A history for current stage
    }
    save_state(path, state)
    return path


def load_state(project_path: str) -> dict:
    state_file = os.path.join(project_path, "state.json")
    if os.path.exists(state_file):
        with open(state_file, encoding="utf-8") as f:
            return json.load(f)
    return {
        "current_stage": "0_setup",
        "last_active": None,
        "tokens_used": 0,
        "conversation": [],
    }


def save_state(project_path: str, state: dict):
    state["last_active"] = datetime.now().isoformat()
    with open(os.path.join(project_path, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def detect_stage(project_path: str) -> str:
    """Detect current stage by checking which artifacts exist (latest first)."""
    for stage, required_files in STAGE_ARTIFACTS:
        all_exist = all(
            os.path.exists(os.path.join(project_path, f))
            for f in required_files
        )
        if all_exist:
            # Return the NEXT stage after the completed one
            idx = STAGE_ORDER.index(stage)
            if idx + 1 < len(STAGE_ORDER):
                return STAGE_ORDER[idx + 1]
            return stage  # last stage
    return "0_setup"


def get_context(project_path: str, max_chars_per_file: int = 2000) -> str:
    """Gather context from project artifacts for LLM prompts."""
    parts = []
    for fname in sorted(os.listdir(project_path)):
        fpath = os.path.join(project_path, fname)
        if fname.endswith((".md", ".json")) and fname != "state.json" and os.path.isfile(fpath):
            with open(fpath, encoding="utf-8") as fh:
                content = fh.read()[:max_chars_per_file]
            parts.append(f"## {fname}\n{content}")
    return "\n\n".join(parts)


def save_artifact(project_path: str, filename: str, content: str):
    filepath = os.path.join(project_path, filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


def next_stage(current: str) -> str | None:
    idx = STAGE_ORDER.index(current)
    if idx + 1 < len(STAGE_ORDER):
        return STAGE_ORDER[idx + 1]
    return None


def count_interview_notes(project_path: str) -> int:
    notes_dir = os.path.join(project_path, "interviews", "notes")
    if not os.path.exists(notes_dir):
        return 0
    return len([f for f in os.listdir(notes_dir) if f.endswith(".md")])
