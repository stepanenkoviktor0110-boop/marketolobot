import asyncio
import logging
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_warned = False


def _sanitize_balance_error(exc: Exception, api_key: str | None = None) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if api_key:
        text = text.replace(api_key, "***")
    return text[:300]


def _load_config() -> dict:
    with _CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


async def check_balance() -> dict | None:
    api_key: str | None = None
    try:
        config = _load_config()
        llm_cfg = config["llm"]["openrouter"]
        api_key = llm_cfg["api_key"]

        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()

        data = response.json().get("data", {})
        limit = data.get("limit")
        usage = float(data.get("usage", 0))
        remaining = (float(limit) - usage) if limit is not None else None
        return {
            "limit": float(limit) if limit is not None else None,
            "usage": usage,
            "remaining": remaining,
        }
    except Exception as exc:
        logger.warning("Balance check failed: %s", _sanitize_balance_error(exc, api_key))
        return None


async def start_periodic_check(bot, owner_id: int):
    global _warned

    while True:
        await asyncio.sleep(4 * 3600)

        config = _load_config()
        threshold = float(
            config.get("llm", {})
            .get("openrouter", {})
            .get("balance_threshold_usd", 1.0)
        )

        result = await check_balance()
        logger.info("OpenRouter balance check result: %s", result)

        if result is None:
            continue

        remaining = result["remaining"]
        if remaining is not None and remaining < threshold and not _warned:
            _warned = True
            await bot.send_message(
                owner_id,
                f"⚠️ OpenRouter: остаток ${remaining:.4f} — меньше ${threshold:.2f}. Пополни баланс.",
            )
        elif remaining is not None and remaining >= threshold:
            _warned = False
