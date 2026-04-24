"""
router.py — Async draft→polish LLM router for the PMF pipeline bot.

Pipeline per stage:
  1. Build context from project files.
  2. Call a fast "draft" model → JSON.
  3. Call a quality "polish" model with the draft → Markdown.
  4. Persist both artifacts and return the polished text.
"""

import json
import logging
import asyncio
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from core.context_builder import (
    build_telegram_context,
    get_source_text,
    save_summary,
    summary_needs_update,
)

from core.storage import get_context, save_artifact
from core.prompts import (
    STAGES,
    get_draft_prompt,
    get_polish_prompt,
    get_artifact_name,
    get_extra_artifact_name,
)
from core.llm_client import llm_call

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def _load_config() -> dict:
    """Load and return the project configuration from config.yaml."""
    with _CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


CONFIG: dict = _load_config()

_LLM_CFG: dict = CONFIG.get("llm", {}).get("claude", CONFIG.get("llm", {}).get("openrouter", {}))


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?|\n?```\s*$", re.IGNORECASE)


def _raise_not_object(text: str, pos: int = 0) -> None:
    raise json.JSONDecodeError("Expected a JSON object", text, pos)


def _parse_json_object(candidate: str, source_text: str, pos: int = 0) -> dict:
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        _raise_not_object(source_text, pos)
    return parsed


def _find_balanced_json_object(text: str) -> Optional[tuple[str, int]]:
    """Return the first balanced ``{...}`` block, ignoring braces inside strings."""
    for start, char in enumerate(text):
        if char != "{":
            continue

        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(text)):
            current = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue

            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    return text[start:idx + 1], start

    return None


def _extract_json(text: str) -> dict:
    """Parse model output as JSON, tolerating markdown fences and surrounding prose."""
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        return _parse_json_object(cleaned, cleaned)
    except json.JSONDecodeError:
        pass

    balanced = _find_balanced_json_object(cleaned)
    if balanced is None:
        raise json.JSONDecodeError("No JSON object found", cleaned, 0)

    candidate, pos = balanced
    return _parse_json_object(candidate, cleaned, pos)


def _log_spend(
    project_path: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    mode: str = "",
    backend: str | None = None,
):
    """Append a spend record to projects/{project}/spend.json."""
    from datetime import datetime
    spend_file = Path(project_path) / "spend.json"
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "mode": mode,
    }
    if backend:
        record["backend"] = backend
    try:
        existing = json.loads(spend_file.read_text(encoding="utf-8")) if spend_file.exists() else []
        existing.append(record)
        spend_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("spend log write failed: %s", exc)

# ---------------------------------------------------------------------------
# Balance check
# ---------------------------------------------------------------------------


async def get_balance() -> dict:
    """Subscription mode — no per-USD balance to report."""
    return {"mode": "subscription", "provider": "claude-code"}


async def _api_call(
    model: str,
    prompt: str,
    system_prompt: Optional[str] = None,
    call_site: str = "stage_draft",
) -> tuple[str, int, int, int]:
    return (await llm_call(call_site, prompt, system_prompt, model_override=model))[:4]


# ---------------------------------------------------------------------------
# Main pipeline function
# ---------------------------------------------------------------------------


async def run_stage(stage: str, user_input: str, project_path: str) -> str:
    """Run a full draft→polish pipeline for *stage*.

    Parameters
    ----------
    stage:
        Stage key matching an entry in ``routing`` in config.yaml,
        e.g. ``"0_setup"``.
    user_input:
        Raw text provided by the user for this stage.
    project_path:
        Absolute (or relative) path to the project directory whose files
        supply additional context.

    Returns
    -------
    str
        Polished Markdown output produced by the polish model.
    """
    routing = CONFIG.get("routing", {})
    if stage not in routing:
        raise KeyError(f"Stage '{stage}' not found in config routing section.")

    draft_model: str = routing[stage]["draft"]
    polish_model: str = routing[stage]["polish"]

    project_path = Path(project_path)

    # ------------------------------------------------------------------
    # 0. Handle manual ("вне бота") stages — no LLM call, stub artifact.
    # ------------------------------------------------------------------
    raw_draft_tpl = (STAGES.get(stage, {}).get("draft_prompt", "") or "").strip()
    raw_polish_tpl = (STAGES.get(stage, {}).get("polish_prompt", "") or "").strip()
    draft_empty = not raw_draft_tpl
    polish_empty = not raw_polish_tpl
    if draft_empty != polish_empty:
        # Fail loud: a half-empty pair is almost certainly a prompts.py edit
        # mistake, not an intentional manual stage.
        raise ValueError(
            f"Stage '{stage}' has one of draft_prompt/polish_prompt empty "
            f"but not the other (draft_empty={draft_empty}, polish_empty={polish_empty}). "
            f"Either set both to '' for a manual stage or fill both."
        )
    if draft_empty and polish_empty:
        stage_name = STAGES.get(stage, {}).get("name", stage)
        logger.info("[%s] Manual stage — skipping LLM, writing stub artifact", stage)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        md = (
            f"# {stage_name}\n\n"
            f"> Этот этап выполняется **вне бота**. Бот не генерирует артефакт автоматически.\n\n"
            f"## Что делать\n\n"
            f"- Проведи действие в реальности (запуск, интервью, сбор данных).\n"
            f"- Результаты загрузи в проект вручную либо через голосовые заметки / контекст группы.\n"
            f"- Обработай контекст через кнопку «Обработать и индексировать» — далее переходи к следующему этапу.\n\n"
            f"_Сформировано автоматически {ts}._\n"
        )
        await asyncio.to_thread(
            save_artifact, str(project_path), f"{stage}_final.md", md
        )
        return md

    # ------------------------------------------------------------------
    # 1. Build context
    # ------------------------------------------------------------------
    logger.info("[%s] Building context from %s", stage, project_path)
    context = await asyncio.to_thread(get_context, str(project_path))

    # ------------------------------------------------------------------
    # 2. Draft call → JSON
    # ------------------------------------------------------------------
    draft_prompt = get_draft_prompt(stage, context, user_input)
    draft_system = (
        "Reply with a single valid JSON object that strictly matches the schema "
        "shown in the user prompt. Do not ask clarifying questions. Do not wrap "
        "the JSON in markdown fences. If a field is unknown, fill it with your "
        "best inference and a low confidence value. Output JSON and nothing else."
    )
    logger.info("[%s] Calling draft model: %s", stage, draft_model)

    raw_draft, draft_p, draft_c, draft_tokens, draft_actual_model, draft_backend = await llm_call(
        "stage_draft", draft_prompt, system_prompt=draft_system, model_override=draft_model
    )
    _log_spend(str(project_path), draft_actual_model, draft_p, draft_c, mode=f"{stage}/draft", backend=draft_backend)
    logger.info("[%s] Draft tokens used: %d", stage, draft_tokens)

    # Parse JSON; retry once on failure
    draft_json: dict
    try:
        draft_json = _extract_json(raw_draft)
    except json.JSONDecodeError:
        logger.warning(
            "[%s] Draft response was not valid JSON — retrying once.", stage
        )
        raw_draft, draft_p, draft_c, draft_tokens, draft_actual_model, draft_backend = await llm_call(
            "stage_draft", draft_prompt, system_prompt=draft_system, model_override=draft_model
        )
        _log_spend(str(project_path), draft_actual_model, draft_p, draft_c, mode=f"{stage}/draft", backend=draft_backend)
        try:
            draft_json = _extract_json(raw_draft)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Draft model '{draft_model}' returned invalid JSON after retry.\n"
                f"Raw content:\n{raw_draft}"
            ) from exc

    # ------------------------------------------------------------------
    # 3. Persist draft artifact
    # ------------------------------------------------------------------
    output_dir = project_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    draft_file = output_dir / f"{stage}_draft.json"
    await asyncio.to_thread(
        draft_file.write_text,
        json.dumps(draft_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("[%s] Draft artifact saved to %s", stage, draft_file)

    # ------------------------------------------------------------------
    # 4. Polish call → Markdown
    # ------------------------------------------------------------------
    polish_prompt = get_polish_prompt(stage, draft_json)
    polish_system = (
        "Return polished Markdown content as your reply. Do not attempt to "
        "create files, run tools, or apologize about permissions — the caller "
        "saves your text to disk. Do not wrap the output in code fences. "
        "Reply with the Markdown body only."
    )
    logger.info("[%s] Calling polish model: %s", stage, polish_model)

    polished_md, polish_p, polish_c, polish_tokens, polish_actual_model, polish_backend = await llm_call(
        "stage_polish", polish_prompt, system_prompt=polish_system, model_override=polish_model
    )
    _log_spend(str(project_path), polish_actual_model, polish_p, polish_c, mode=f"{stage}/polish", backend=polish_backend)
    logger.info("[%s] Polish tokens used: %d", stage, polish_tokens)

    # ------------------------------------------------------------------
    # 5. Persist final artifact
    # ------------------------------------------------------------------
    artifact_name = get_artifact_name(stage)
    await asyncio.to_thread(save_artifact, str(project_path), artifact_name, polished_md)
    logger.info("[%s] Final artifact saved: %s", stage, artifact_name)

    # Optionally persist extra artifact if the stage defines one
    extra_name = get_extra_artifact_name(stage)
    if extra_name:
        await asyncio.to_thread(save_artifact, str(project_path), extra_name, polished_md)
        logger.info("[%s] Extra artifact saved: %s", stage, extra_name)

    return polished_md


# ---------------------------------------------------------------------------
# Telegram lite: summarize + respond
# ---------------------------------------------------------------------------

_DRAFT_MODEL = _LLM_CFG.get("draft_model") or list(CONFIG.get("routing", {}).values())[0]["draft"]
_POLISH_MODEL = _LLM_CFG.get("polish_model") or list(CONFIG.get("routing", {}).values())[0]["polish"]


async def ensure_summary(project_path: str):
    """Regenerate project_summary.md via DeepSeek if source files changed."""
    if not await asyncio.to_thread(summary_needs_update, project_path):
        return
    source_text = await asyncio.to_thread(get_source_text, project_path)
    if not source_text:
        return
    prompt = (
        "Сожми следующие материалы проекта в краткое резюме на 500-600 символов. "
        "Сохрани суть: продукт, целевая аудитория, ключевые идеи, текущий статус.\n\n"
        f"{source_text}"
    )
    summary_text, sum_p, sum_c, _, summary_model, summary_backend = await llm_call(
        "summary", prompt, model_override=_POLISH_MODEL
    )
    _log_spend(project_path, summary_model, sum_p, sum_c, mode="summary", backend=summary_backend)
    await asyncio.to_thread(save_summary, project_path, summary_text.strip())
    logger.info("Summary regenerated for project: %s", project_path)


async def telegram_reply(user_text: str, project_path: str, mode: str = "chat", username: str | None = None) -> str:
    """Single draft-model call with compact project context for Telegram.

    mode: "chat" | "hypothesize" | "brainstorm" | "rate"
    """
    await ensure_summary(project_path)
    # For artifact modes, use only project summary — no chat history to avoid repeating old results
    if mode in ("hypothesize", "brainstorm", "rate"):
        from core.context_builder import get_summary
        raw_summary = await asyncio.to_thread(get_summary, project_path)
        context = f"=== Проект (summary) ===\n{raw_summary}" if raw_summary else ""
    else:
        context = await asyncio.to_thread(build_telegram_context, project_path)

    persona = (
        "Ты — маркетолог-энтузиаст, помогаешь с PMF-анализом. "
        "Говоришь просто и по делу, но каждое сообщение наполнено эмоциями через эмодзи — "
        "они отражают твоё реальное отношение к тому что говоришь. "
        "Не перегружай текст, но эмодзи ставь щедро и к месту. "
        "Опирайся на контекст проекта.\n\n"
        "ВАЖНО: отвечай ТОЛЬКО на русском языке. Никогда не используй другие языки, "
        "даже если в контексте встретился текст на другом языке.\n"
    )
    system_prompts = {
        "chat": persona + "Отвечай на текущее сообщение пользователя. История чата — это контекст, а не очередь вопросов: не отвечай на старые вопросы из неё, если пользователь сейчас не просит. Если сообщение — похвала, шутка или реплика — отвечай именно на неё, без разбора проекта. Никогда не перечисляй и не повторяй гипотезы, идеи или оценки из контекста — пользователь знает где их найти. Будь краток. НИКОГДА не здоровайся (никаких 'привет', '👋', 'добрый день') — пользователь уже в разговоре, отвечай сразу по делу.",
        "hypothesize": persona + "Сгенерируй 3-5 чётких гипотез. Формат: нумерованный список. В НАЧАЛЕ ответа обязательно выдели секцию '⚠️ Риски' с перечислением ключевых рисков и что может пойти не так.",
        "brainstorm": persona + "Проведи мозговой штурм: дай 5-7 конкретных идей. Укажи риски для каждой.",
        "rate": persona + "Оцени идею по шкале 1-10. Дай обоснование, одну рекомендацию и выдели ключевые риски.",
    }

    system = system_prompts.get(mode, system_prompts["chat"])
    if context:
        system += f"\n\n{context}"

    # Addressing: collective "Валеричи" ONLY in groups, personal name in DMs
    has_group_ctx = "=== Последние сообщения группы ===" in (context or "")

    if has_group_ctx:
        # Group chat — use collective address
        collective = random.choice(["Валеричи", "Уважаемые Валеричи", "Господа Валеричи"])
        system += f"\n\nТы отвечаешь группе. Используй обращение «{collective}»."
    elif username:
        # Private chat — address user personally
        system += f"\n\nС тобой сейчас работает: {username}. Обращайся к нему по имени, на 'ты' или уважительно, но НЕ используй коллективные обращения."

    system += (
        f"\n\nТекущее сообщение (отвечай именно на него): {user_text}"
    )

    call_site = "telegram_chat" if mode == "chat" else "telegram_artifact"
    response, resp_p, resp_c, tokens, actual_model, backend = await llm_call(
        call_site, user_text, system_prompt=system, model_override=_DRAFT_MODEL
    )
    _log_spend(project_path, actual_model, resp_p, resp_c, mode=mode, backend=backend)
    logger.info("telegram_reply mode=%s tokens=%d project=%s", mode, tokens, project_path)

    # Persist non-chat responses as timestamped artifact files (atomic, no race)
    _artifact_dirs = {
        "hypothesize": "hypotheses",
        "brainstorm":  "brainstorm",
        "rate":        "ratings",
    }
    if mode in _artifact_dirs:
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        subdir = Path(project_path) / _artifact_dirs[mode]
        subdir.mkdir(exist_ok=True)
        artifact = subdir / f"{ts}.md"
        header = f"## {datetime.now().strftime('%Y-%m-%d %H:%M')} | {user_text[:80]}\n\n"
        await asyncio.to_thread(artifact.write_text, header + response, encoding="utf-8")
        logger.info("artifact saved: %s", artifact)

    return response


# ---------------------------------------------------------------------------
# Voice post-processing: transcribe → summarize + extract actions
# ---------------------------------------------------------------------------


async def summarize_voice_message(transcript: str, project_path: str) -> str:
    """Take a voice transcript and return: summary + extracted actions/decisions."""
    await ensure_summary(project_path)
    from core.context_builder import get_summary
    raw_summary = await asyncio.to_thread(get_summary, project_path)
    context_block = f"=== Проект (summary) ===\n{raw_summary}" if raw_summary else ""

    system = (
        "Ты — ассистент-аналитик. Тебе дали транскрипцию голосового сообщения из проекта.\n"
        "Сделай следующее:\n"
        "1. 📝 **Краткое саммари** — 2-3 предложения, суть сказанного.\n"
        "2. 🎯 **Извлечённые действия/решения** — список конкретных шагов, решений или вопросов, "
        "которые нужно проработать. Если ничего конкретного нет — так и напиши.\n"
        "3. 💡 **Рекомендация** — что бот/PMF-процесс может предложить на основе этого.\n"
        "Отвечай ТОЛЬКО на русском. Будь краток.\n"
        f"\n{context_block}"
    )

    user_prompt = (
        f"Вот транскрипция голосового:\n\n{transcript}\n\n"
        "Проанализируй в контексте проекта и выдай результат по структуре выше."
    )

    response, resp_p, resp_c, tokens, actual_model, backend = await llm_call(
        "voice_summary", user_prompt, system_prompt=system, model_override=_DRAFT_MODEL
    )
    _log_spend(project_path, actual_model, resp_p, resp_c, mode="voice_summary", backend=backend)
    return response
