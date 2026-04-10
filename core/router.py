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
import os
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

from core.storage import get_context, save_artifact, load_state, save_state
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

# ---------------------------------------------------------------------------
# Balance check
# ---------------------------------------------------------------------------


async def get_balance() -> dict:
    """Return OpenRouter credit balance info."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_BASE_URL}/auth/key",
            headers={"Authorization": f"Bearer {_API_KEY}"},
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Low-level API helper
# ---------------------------------------------------------------------------


async def _api_call(
    model: str,
    prompt: str,
    system_prompt: Optional[str] = None,
) -> tuple[str, int]:
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
    tuple[str, int]
        ``(content, tokens_used)`` where *content* is the raw text returned
        by the model and *tokens_used* is the total token count from the
        response usage field (0 if unavailable).

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
            f"{response.text}"
        )

    data = response.json()
    content: str = data["choices"][0]["message"]["content"]
    tokens_used: int = data.get("usage", {}).get("total_tokens", 0)
    return content, tokens_used


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
    context = get_context(str(project_path))

    # ------------------------------------------------------------------
    # 2. Draft call → JSON
    # ------------------------------------------------------------------
    draft_prompt = get_draft_prompt(stage, context, user_input)
    logger.info("[%s] Calling draft model: %s", stage, draft_model)

    raw_draft, draft_tokens = await _api_call(draft_model, draft_prompt)
    logger.info("[%s] Draft tokens used: %d", stage, draft_tokens)

    # Parse JSON; retry once on failure
    draft_json: dict
    try:
        draft_json = json.loads(raw_draft)
    except json.JSONDecodeError:
        logger.warning(
            "[%s] Draft response was not valid JSON — retrying once.", stage
        )
        raw_draft, draft_tokens = await _api_call(draft_model, draft_prompt)
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
    draft_file.write_text(json.dumps(draft_json, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[%s] Draft artifact saved to %s", stage, draft_file)

    # ------------------------------------------------------------------
    # 4. Polish call → Markdown
    # ------------------------------------------------------------------
    polish_prompt = get_polish_prompt(stage, draft_json)
    logger.info("[%s] Calling polish model: %s", stage, polish_model)

    polished_md, polish_tokens = await _api_call(polish_model, polish_prompt)
    logger.info("[%s] Polish tokens used: %d", stage, polish_tokens)

    # ------------------------------------------------------------------
    # 5. Persist final artifact
    # ------------------------------------------------------------------
    artifact_name = get_artifact_name(stage)
    save_artifact(str(project_path), artifact_name, polished_md)
    logger.info("[%s] Final artifact saved: %s", stage, artifact_name)

    # Optionally persist extra artifact if the stage defines one
    extra_name = get_extra_artifact_name(stage)
    if extra_name:
        save_artifact(str(project_path), extra_name, polished_md)
        logger.info("[%s] Extra artifact saved: %s", stage, extra_name)

    return polished_md


# ---------------------------------------------------------------------------
# Telegram lite: summarize + respond
# ---------------------------------------------------------------------------

_DRAFT_MODEL = _LLM_CFG.get("draft_model") or list(CONFIG.get("routing", {}).values())[0]["draft"]
_POLISH_MODEL = _LLM_CFG.get("polish_model") or list(CONFIG.get("routing", {}).values())[0]["polish"]


async def ensure_summary(project_path: str):
    """Regenerate project_summary.md via DeepSeek if source files changed."""
    if not summary_needs_update(project_path):
        return
    source_text = get_source_text(project_path)
    if not source_text:
        return
    prompt = (
        "Сожми следующие материалы проекта в краткое резюме на 500-600 символов. "
        "Сохрани суть: продукт, целевая аудитория, ключевые идеи, текущий статус.\n\n"
        f"{source_text}"
    )
    summary, _ = await _api_call(_POLISH_MODEL, prompt)
    save_summary(project_path, summary.strip())
    logger.info("Summary regenerated for project: %s", project_path)


async def telegram_reply(user_text: str, project_path: str, mode: str = "chat") -> str:
    """Single draft-model call with compact project context for Telegram.

    mode: "chat" | "hypothesize" | "brainstorm" | "rate"
    """
    await ensure_summary(project_path)
    context = build_telegram_context(project_path)

    persona = (
        "Ты — маркетолог-энтузиаст, помогаешь с PMF-анализом. "
        "Говоришь просто и по делу, но каждое сообщение наполнено эмоциями через эмодзи — "
        "они отражают твоё реальное отношение к тому что говоришь. "
        "Не перегружай текст, но эмодзи ставь щедро и к месту. "
        "Опирайся на контекст проекта.\n\n"
    )
    system_prompts = {
        "chat": persona + "Отвечай кратко и по делу.",
        "hypothesize": persona + "Сгенерируй 3-5 чётких гипотез. Формат: нумерованный список.",
        "brainstorm": persona + "Проведи мозговой штурм: дай 5-7 конкретных идей.",
        "rate": persona + "Оцени идею по шкале 1-10. Дай обоснование и одну рекомендацию.",
    }

    system = system_prompts.get(mode, system_prompts["chat"])
    if context:
        system += f"\n\n{context}"

    response, tokens = await _api_call(_DRAFT_MODEL, user_text, system_prompt=system)
    logger.info("telegram_reply mode=%s tokens=%d project=%s", mode, tokens, project_path)
    return response
