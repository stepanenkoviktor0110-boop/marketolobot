"""Balance monitor — disabled in subscription mode (Claude Code CLI).

Kept as a stub so existing imports do not break. Returns ``None`` for balance
checks and runs an idle loop for the periodic task so the bot can keep relying
on the same call sites.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def check_balance() -> dict | None:
    return None


async def start_periodic_check(bot, owner_id: int):
    logger.info("Balance monitor disabled (Claude Code subscription mode).")
    while True:
        await asyncio.sleep(24 * 3600)
