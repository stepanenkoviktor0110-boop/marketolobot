import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import httpx
import yaml

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
_ENV_PATH = _BASE_DIR / ".env"
_CONFIG_PATH = _BASE_DIR / "config.yaml"


def _load_env_file() -> None:
    if not _ENV_PATH.exists():
        logger.warning(".env file not found at %s", _ENV_PATH)
        return

    try:
        for raw_line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip()
    except Exception as exc:
        logger.warning("failed to load .env from %s: %s", _ENV_PATH, exc)


def _load_config() -> dict:
    with _CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


_load_env_file()
CONFIG: dict = _load_config()

_CLAUDE_CFG: dict = CONFIG.get("llm", {}).get("claude", {})
_OPENROUTER_CFG: dict = CONFIG.get("llm", {}).get("openrouter", {})

_CLAUDE_TIMEOUT: int = int(_CLAUDE_CFG.get("timeout", 180))
_CLAUDE_BIN: str = (
    os.getenv("CLAUDE_BIN")
    or _CLAUDE_CFG.get("bin")
    or shutil.which("claude")
    or "/home/xander_bot/.npm-global/bin/claude"
)
_CLAUDE_CWD: str = os.getenv("CLAUDE_CWD") or tempfile.gettempdir()

_OPENROUTER_TIMEOUT: int = int(_OPENROUTER_CFG.get("timeout", 60))
_OPENROUTER_BASE_URL: str = _OPENROUTER_CFG.get("base_url", "https://openrouter.ai/api/v1")
_OPENROUTER_API_ENV: str = _OPENROUTER_CFG.get("api_key_env", "OPENROUTER_API_KEY")

_MODEL_ALIASES = {
    "qwen/qwen-2.5-72b-instruct": "haiku",
    "deepseek/deepseek-chat-v3-0324": "sonnet",
}

_OPENROUTER_PROBE_LOGGED = False


def _resolve_model(name: str) -> str:
    if not name:
        return "sonnet"
    return _MODEL_ALIASES.get(name, name)


async def call_via_claude_cli(
    model: str,
    prompt: str,
    system_prompt: Optional[str] = None,
) -> tuple[str, int, int, int]:
    cmd = [
        _CLAUDE_BIN,
        "--print",
        "--no-session-persistence",
        "--tools", "",
        "--disable-slash-commands",
        "--output-format", "json",
        "--model", _resolve_model(model),
    ]
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=_CLAUDE_CWD,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Claude CLI not found at {_CLAUDE_BIN}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=_CLAUDE_TIMEOUT,
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise TimeoutError(
            f"Claude CLI timed out after {_CLAUDE_TIMEOUT}s for model '{model}'."
        ) from exc

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Claude CLI exited with code {proc.returncode} for model '{model}': {err[:500]}"
        )

    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        snippet = stdout[:500].decode("utf-8", errors="replace")
        raise RuntimeError(f"Claude CLI returned non-JSON output: {snippet!r}") from exc

    if payload.get("is_error"):
        raise RuntimeError(
            f"Claude CLI reported error for model '{model}': "
            f"{str(payload.get('result', ''))[:500]}"
        )

    content: str = payload.get("result", "") or ""
    usage = payload.get("usage", {}) or {}
    input_tokens = int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    total_tokens = input_tokens + output_tokens
    return content, input_tokens, output_tokens, total_tokens


async def call_via_openrouter(
    model: str,
    prompt: str,
    system_prompt: Optional[str] = None,
) -> tuple[str, int, int, int]:
    api_key = os.getenv(_OPENROUTER_API_ENV)
    if not api_key:
        raise RuntimeError(f"Missing {_OPENROUTER_API_ENV}")

    messages = []
    if system_prompt is not None:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    async with httpx.AsyncClient(timeout=_OPENROUTER_TIMEOUT) as client:
        response = await client.post(
            f"{_OPENROUTER_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/local/marketbot",
                "X-Title": "MarketBot",
            },
            json={
                "model": model,
                "messages": messages,
            },
        )
        response.raise_for_status()

    payload = response.json()
    choices = payload.get("choices")
    if not choices:
        raise ValueError("OpenRouter response missing choices")

    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    content = message.get("content")
    if not content:
        raise ValueError("OpenRouter response missing message content")

    usage = payload.get("usage", {}) or {}
    input_tokens = int(usage.get("prompt_tokens", 0))
    output_tokens = int(usage.get("completion_tokens", 0))
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens))
    return content, input_tokens, output_tokens, total_tokens


def _claude_default_model() -> str:
    return _CLAUDE_CFG.get("draft_model", "haiku")


def _log_openrouter_probe(models: list[str]) -> None:
    global _OPENROUTER_PROBE_LOGGED
    if _OPENROUTER_PROBE_LOGGED:
        return
    logger.info("OpenRouter configured, models available: %s", models)
    _OPENROUTER_PROBE_LOGGED = True


async def llm_call(
    call_site: str,
    prompt: str,
    system_prompt: str | None = None,
    model_override: str | None = None,
) -> tuple[str, int, int, int, str, str]:
    llm_cfg = CONFIG.get("llm", {})
    backend = llm_cfg.get("call_sites", {}).get(call_site, "claude_cli")
    fallback_model = model_override or _claude_default_model()

    if backend == "openrouter_free":
        models = list(_OPENROUTER_CFG.get("models", []) or [])
        api_key = os.getenv(_OPENROUTER_API_ENV)
        if api_key and models:
            _log_openrouter_probe(models)
            last_exc: Exception | None = None
            for model_name in models:
                try:
                    content, in_tok, out_tok, total = await call_via_openrouter(
                        model_name,
                        prompt,
                        system_prompt=system_prompt,
                    )
                    return content, in_tok, out_tok, total, model_name, "openrouter_free"
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "OpenRouter call failed for call_site=%s model=%s: %s",
                        call_site,
                        model_name,
                        exc,
                    )
            logger.warning(
                "Falling back to Claude CLI for call_site=%s after OpenRouter failures: %s",
                call_site,
                last_exc,
            )
        else:
            reason = f"missing {_OPENROUTER_API_ENV}" if not api_key else "no OpenRouter models configured"
            logger.warning("Falling back to Claude CLI for call_site=%s: %s", call_site, reason)

    content, in_tok, out_tok, total = await call_via_claude_cli(
        fallback_model,
        prompt,
        system_prompt=system_prompt,
    )
    return content, in_tok, out_tok, total, _resolve_model(fallback_model), "claude_cli"
