"""
Builds compact context for Telegram lite-mode LLM calls.

Strategy:
- project_summary.md: auto-generated summary of project files (cached, regenerated on mtime change)
- group_context.md: last 50 lines only
- Total target: ~2000 chars
"""

import os
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SUMMARY_MAX_CHARS = 700
GROUP_CTX_TAIL_LINES = 50
SUMMARY_META_FILE = "project_summary.meta.json"


def _source_files(project_path: str) -> list[Path]:
    """Return project .md files excluding generated ones."""
    excluded = {"group_context.md", "project_summary.md"}
    root = Path(project_path)
    return [
        f for f in root.glob("*.md")
        if f.name not in excluded and f.is_file()
    ]


def _sources_mtime(project_path: str) -> float:
    files = _source_files(project_path)
    if not files:
        return 0.0
    return max(f.stat().st_mtime for f in files)


def _load_summary_meta(project_path: str) -> dict:
    meta_path = Path(project_path) / SUMMARY_META_FILE
    if meta_path.exists():
        with open(meta_path) as f:
            return json.load(f)
    return {}


def _save_summary_meta(project_path: str, mtime: float):
    meta_path = Path(project_path) / SUMMARY_META_FILE
    with open(meta_path, "w") as f:
        json.dump({"source_mtime": mtime}, f)


def summary_needs_update(project_path: str) -> bool:
    summary_path = Path(project_path) / "project_summary.md"
    if not summary_path.exists():
        return True
    meta = _load_summary_meta(project_path)
    cached_mtime = meta.get("source_mtime", 0)
    return _sources_mtime(project_path) > cached_mtime


def get_source_text(project_path: str, max_chars: int = 6000) -> str:
    """Concatenate source .md files for summarization input."""
    parts = []
    total = 0
    for f in sorted(_source_files(project_path)):
        content = f.read_text(encoding="utf-8")
        if total + len(content) > max_chars:
            content = content[:max_chars - total]
        parts.append(f"## {f.name}\n{content}")
        total += len(content)
        if total >= max_chars:
            break
    return "\n\n".join(parts)


def save_summary(project_path: str, summary: str):
    summary_path = Path(project_path) / "project_summary.md"
    summary_path.write_text(summary, encoding="utf-8")
    _save_summary_meta(project_path, _sources_mtime(project_path))
    logger.info("project_summary.md updated (project=%s)", Path(project_path).name)


def get_summary(project_path: str) -> str:
    summary_path = Path(project_path) / "project_summary.md"
    if summary_path.exists():
        return summary_path.read_text(encoding="utf-8")[:SUMMARY_MAX_CHARS]
    return ""


def get_group_context_tail(project_path: str) -> str:
    ctx_path = Path(project_path) / "group_context.md"
    if not ctx_path.exists():
        return ""
    lines = ctx_path.read_text(encoding="utf-8").splitlines()
    tail = lines[-GROUP_CTX_TAIL_LINES:]
    return "\n".join(tail)


def build_telegram_context(project_path: str) -> str:
    """Assemble compact context for Telegram lite calls (~2000 chars)."""
    parts = []

    summary = get_summary(project_path)
    if summary:
        parts.append(f"=== Проект (summary) ===\n{summary}")

    group_ctx = get_group_context_tail(project_path)
    if group_ctx:
        parts.append(f"=== Последние сообщения группы ===\n{group_ctx}")

    return "\n\n".join(parts)
