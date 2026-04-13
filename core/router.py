"""
router.py — Async draft→polish LLM router for the PMF pipeline bot.

All LLM calls go through the OpenRouter API (single endpoint, single token).
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
from pathlib import Path
from typing import Optional

import httpx
import yaml

from core.context_builder import (
    build_telegram_context,
    get_source_text,
    save_summary,
    summary_needs_update,
)

from core.storage import get_context, save_artifact
from core.prompts import (
    get_draft_prompt,
    get_polish_prompt,
    get_artifact_name,
    get_extra_artifact_name,
)

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

_LLM_CFG: dict = CONFIG["llm"]["openrouter"]
_BASE_URL: str = _LLM_CFG["base_url"].rstrip("/")
_API_KEY: str = _LLM_CFG["api_key"]
_TIMEOUT: int = int(_LLM_CFG.get("timeout", 120))


def _sanitize_api_error(text: str) -> str:
    sanitized = text.replace(_API_KEY, "***") if _API_KEY else text
    return sanitized[:500]


def _log_spend(project_path: str, model: str, prompt_tokens: int, completion_tokens: int, mode: str = ""):
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
    """Return OpenRouter credit balance info."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_BASE_URL}/auth/key",
                headers={"Authorization": f"Bearer {_API_KEY}"},
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        logger.warning("OpenRouter balance request failed: %s", exc.__class__.__name__)
        raise RuntimeError("OpenRouter balance request failed") from exc


# ---------------------------------------------------------------------------
# Low-level API helper
# ---------------------------------------------------------------------------


async def _api_call(
    model: str,
    prompt: str,
    system_prompt: Optional[str] = None,
) -> tuple[str, int, int, int]:
    """POST a single chat completion request to OpenRouter.

    Parameters
    ----------
    model:
        OpenRouter model identifier, e.g. ``deepseek/deepseek-chat-v3-0324``.
    prompt:
        The user-role message content.
    system_prompt:
        Optional system-role message content.

    Returns
    -------
    tuple[str, int, int, int]
        ``(content, prompt_tokens, completion_tokens, total_tokens)`` where
        *content* is the raw text returned by the model and the token counts
        come from the response usage field (0 if unavailable).

    Raises
    ------
    httpx.TimeoutException
        Re-raised with a descriptive message when the request times out.
    RuntimeError
        When the API returns a 4xx or 5xx status.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.5,
        "max_tokens": 4000,
    }

    headers = {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/pmf-pipeline-bot",
    }

    url = f"{_BASE_URL}/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise httpx.TimeoutException(
            f"OpenRouter request timed out after {_TIMEOUT}s for model '{model}'."
        ) from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"OpenRouter API error {response.status_code} for model '{model}': "
            f"{_sanitize_api_error(response.text)}"
        )

    data = response.json()
    content: str = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    prompt_tokens: int = usage.get("prompt_tokens", 0)
    completion_tokens: int = usage.get("completion_tokens", 0)
    total_tokens: int = usage.get("total_tokens", prompt_tokens + completion_tokens)
    return content, prompt_tokens, completion_tokens, total_tokens


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
    # 1. Build context
    # ------------------------------------------------------------------
    logger.info("[%s] Building context from %s", stage, project_path)
    context = await asyncio.to_thread(get_context, str(project_path))

    # ------------------------------------------------------------------
    # 2. Draft call → JSON
    # ------------------------------------------------------------------
    draft_prompt = get_draft_prompt(stage, context, user_input)
    logger.info("[%s] Calling draft model: %s", stage, draft_model)

    raw_draft, draft_p, draft_c, draft_tokens = await _api_call(draft_model, draft_prompt)
    _log_spend(str(project_path), draft_model, draft_p, draft_c, mode=f"{stage}/draft")
    logger.info("[%s] Draft tokens used: %d", stage, draft_tokens)

    # Parse JSON; retry once on failure
    draft_json: dict
    try:
        draft_json = json.loads(raw_draft)
    except json.JSONDecodeError:
        logger.warning(
            "[%s] Draft response was not valid JSON — retrying once.", stage
        )
        raw_draft, draft_p, draft_c, draft_tokens = await _api_call(draft_model, draft_prompt)
        _log_spend(str(project_path), draft_model, draft_p, draft_c, mode=f"{stage}/draft")
        try:
            draft_json = json.loads(raw_draft)
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
    logger.info("[%s] Calling polish model: %s", stage, polish_model)

    polished_md, polish_p, polish_c, polish_tokens = await _api_call(polish_model, polish_prompt)
    _log_spend(str(project_path), polish_model, polish_p, polish_c, mode=f"{stage}/polish")
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
    summary_text, sum_p, sum_c, _ = await _api_call(_POLISH_MODEL, prompt)
    _log_spend(project_path, _POLISH_MODEL, sum_p, sum_c, mode="summary")
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

    response, resp_p, resp_c, tokens = await _api_call(_DRAFT_MODEL, user_text, system_prompt=system)
    _log_spend(project_path, _DRAFT_MODEL, resp_p, resp_c, mode=mode)
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

    response, resp_p, resp_c, tokens = await _api_call(_DRAFT_MODEL, user_prompt, system_prompt=system)
    _log_spend(project_path, _DRAFT_MODEL, resp_p, resp_c, mode="voice_summary")
    return response
