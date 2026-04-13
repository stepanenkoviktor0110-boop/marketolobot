import json
import os
from datetime import datetime
from pathlib import Path


# Max messages kept in group_context.md (rolling window)
GROUP_CONTEXT_MAX_LINES = 300


def load_group_links(projects_root: str) -> dict:
    """Return {chat_id: project_name} mapping."""
    path = Path(projects_root) / "group_links.json"
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_group_links(projects_root: str, links: dict):
    path = Path(projects_root) / "group_links.json"
    os.makedirs(projects_root, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(links, f, indent=2, ensure_ascii=False)


def link_group(projects_root: str, chat_id: int, project_name: str):
    links = load_group_links(projects_root)
    links[str(chat_id)] = project_name
    save_group_links(projects_root, links)


def get_linked_project(projects_root: str, chat_id: int) -> str | None:
    links = load_group_links(projects_root)
    return links.get(str(chat_id))


def get_user_display_name(project_path: str, user_id: int, fallback: str) -> str:
    """Return display name from users.json registry, or fallback."""
    registry_path = Path(project_path) / "users.json"
    if registry_path.exists():
        with registry_path.open(encoding="utf-8") as f:
            registry = json.load(f)
        name = registry.get(str(user_id))
        if name:
            return name
    return fallback


def append_group_message(project_path: str, username: str, text: str):
    """Append a group message to group_context.md with a rolling window."""
    ctx_file = Path(project_path) / "group_context.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"[{timestamp}] {username}: {text}\n"

    if ctx_file.exists():
        with ctx_file.open(encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = []

    lines.append(line)

    if len(lines) > GROUP_CONTEXT_MAX_LINES:
        lines = lines[-GROUP_CONTEXT_MAX_LINES:]

    with ctx_file.open("w", encoding="utf-8") as f:
        f.writelines(lines)
