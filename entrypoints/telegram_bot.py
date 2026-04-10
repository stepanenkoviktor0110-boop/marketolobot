"""PMF Pipeline Telegram Bot — aiogram 3.x"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, FSInputFile, Voice

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import storage, prompts
from core.router import run_stage, get_balance, telegram_reply
from core.transcriber import transcribe_ogg
from core.processor import chunk_text, format_status, format_stage_intro

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = PROJECT_ROOT / "config.yaml"
with open(CONFIG_PATH, encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

OWNER_ID = cfg["owner_id"]
PROJECTS_ROOT = str(PROJECT_ROOT / cfg["projects_root"])
os.makedirs(PROJECTS_ROOT, exist_ok=True)

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

bot = Bot(token=cfg["bot"]["token"])
dp = Dispatcher()
router_tg = Router()
dp.include_router(router_tg)

# ---------------------------------------------------------------------------
# Per-user in-memory state
# ---------------------------------------------------------------------------

user_state: dict[int, dict] = {}
user_semaphores: dict[int, asyncio.Semaphore] = {}


def get_ustate(user_id: int) -> dict:
    if user_id not in user_state:
        user_state[user_id] = {
            "active_project": None,
            "question_index": 0,
            "answers": [],
            "pending_action": None,  # "new" | "use" | None
        }
    return user_state[user_id]


def get_semaphore(user_id: int) -> asyncio.Semaphore:
    if user_id not in user_semaphores:
        user_semaphores[user_id] = asyncio.Semaphore(1)
    return user_semaphores[user_id]


def is_allowed(message: Message) -> bool:
    """Allow anyone in group chats; restrict private chats to owner only."""
    if message.chat.type in ("group", "supergroup"):
        return True
    return message.from_user.id == OWNER_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_active_project(message: Message) -> str | None:
    """Return active project: group-linked project or user's personal active project."""
    if message.chat.type in ("group", "supergroup"):
        return storage.get_linked_project(PROJECTS_ROOT, message.chat.id)
    return get_ustate(message.from_user.id)["active_project"]


async def send_long(message: Message, text: str):
    """Send text split into Telegram-safe chunks. First chunk replies to original message."""
    chunks = chunk_text(text)
    for i, chunk in enumerate(chunks):
        if i == 0:
            await message.reply(chunk)
        else:
            await message.answer(chunk)


async def enter_stage(message: Message, project_path: str, stage: str):
    """Enter a stage: send intro, ask first question or show manual instructions."""
    ustate = get_ustate(message.from_user.id)
    ustate["question_index"] = 0
    ustate["answers"] = []

    stage_name = storage.STAGE_NAMES_RU.get(stage, stage)
    questions = prompts.get_stage_questions(stage)

    if prompts.is_manual_stage(stage):
        if stage == "6_field":
            notes = storage.count_interview_notes(project_path)
            await message.reply(
                f"📋 Этап: {stage_name}\n\n"
                f"Этот этап выполняется вне бота.\n"
                f"Проведи 15-20 интервью, сохрани заметки в папку проекта.\n"
                f"Сейчас заметок: {notes}\n\n"
                f"Когда закончишь — жми /skip"
            )
        else:  # 8_mvp_launch
            await message.reply(
                f"📋 Этап: {stage_name}\n\n"
                f"Этот этап выполняется вне бота.\n"
                f"Запусти MVP, набери ~40 активных пользователей.\n\n"
                f"Когда будут данные — жми /skip"
            )
        return

    intro = format_stage_intro(stage, stage_name, questions)
    await message.reply(intro)

    if questions:
        await message.reply(f"❓ Вопрос 1/{len(questions)}:\n\n{questions[0]}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@router_tg.message(CommandStart())
async def cmd_start(message: Message):
    if not is_allowed(message):
        return
    await message.reply(
        "👋 PMF Pipeline Bot\n\n"
        "Команды:\n"
        "/new <name> — создать проект\n"
        "/use <name> — выбрать проект\n"
        "/projects — список проектов\n"
        "/continue — продолжить этап\n"
        "/status — текущий статус\n"
        "/export — выгрузить артефакты\n"
        "/skip — пропустить ручной этап"
    )


async def _do_new_project(message: Message, name: str):
    name = name.strip().lower().replace(" ", "-")
    existing = storage.list_projects(PROJECTS_ROOT)
    if name in existing:
        await message.reply(f"Проект '{name}' уже существует. Используй /use {name}")
        return

    project_path = storage.create_project(PROJECTS_ROOT, name)
    ustate = get_ustate(message.from_user.id)
    ustate["active_project"] = name

    await message.reply(f"✅ Проект '{name}' создан!")

    state = storage.load_state(project_path)
    await enter_stage(message, project_path, state["current_stage"])


@router_tg.message(Command("balance"))
async def cmd_balance(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    try:
        data = await get_balance()
        d = data.get("data", {})
        limit = d.get("limit")
        usage = d.get("usage", 0)
        remaining = (limit - usage) if limit else None

        lines = ["💳 OpenRouter баланс\n"]
        if limit is not None:
            lines.append(f"Лимит: ${limit:.4f}")
            lines.append(f"Использовано: ${usage:.4f}")
            lines.append(f"Остаток: ${remaining:.4f}")
        else:
            lines.append(f"Использовано: ${usage:.4f}")
            lines.append("Лимит: не установлен (pay-as-you-go)")
        await message.reply("\n".join(lines))
    except Exception as e:
        await message.reply(f"Ошибка: {e}")


@router_tg.message(Command("link_project"))
async def cmd_link_project(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Команда только для групп.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Формат: /link_project <имя_проекта>")
        return

    name = parts[1].strip().lower().replace(" ", "-")
    existing = storage.list_projects(PROJECTS_ROOT)
    if name not in existing:
        storage.create_project(PROJECTS_ROOT, name)
        await message.reply(f"✅ Проект '{name}' создан и привязан к этой группе.")
    else:
        await message.reply(f"✅ Группа привязана к проекту '{name}'.")

    storage.link_group(PROJECTS_ROOT, message.chat.id, name)


@router_tg.message(Command("new"))
async def cmd_new(message: Message):
    if not is_allowed(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        ustate = get_ustate(message.from_user.id)
        ustate["pending_action"] = "new"
        await message.reply("Введи имя нового проекта:")
        return

    await _do_new_project(message, parts[1])


async def _do_use_project(message: Message, name: str):
    name = name.strip()
    existing = storage.list_projects(PROJECTS_ROOT)
    if name not in existing:
        await message.reply(
            f"Проект '{name}' не найден.\n"
            f"Доступные: {', '.join(existing) if existing else 'нет проектов'}"
        )
        return

    ustate = get_ustate(message.from_user.id)
    ustate["active_project"] = name

    project_path = storage.get_project_path(PROJECTS_ROOT, name)
    state = storage.load_state(project_path)
    stage = state["current_stage"]
    stage_name = storage.STAGE_NAMES_RU.get(stage, stage)

    await message.reply(format_status(name, stage, stage_name))
    await message.reply("Жми /continue чтобы продолжить этап.")


@router_tg.message(Command("use"))
async def cmd_use(message: Message):
    if not is_allowed(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        existing = storage.list_projects(PROJECTS_ROOT)
        ustate = get_ustate(message.from_user.id)
        ustate["pending_action"] = "use"
        if existing:
            await message.reply(
                f"Введи имя проекта:\n\nДоступные: {', '.join(existing)}"
            )
        else:
            await message.reply("Нет проектов. Создай: /new")
            ustate["pending_action"] = None
        return

    await _do_use_project(message, parts[1])


@router_tg.message(Command("projects"))
async def cmd_projects(message: Message):
    if not is_allowed(message):
        return
    projects = storage.list_projects(PROJECTS_ROOT)
    if not projects:
        await message.reply("Нет проектов. Создай: /new <имя>")
        return

    lines = ["📁 Проекты:\n"]
    for p in projects:
        path = storage.get_project_path(PROJECTS_ROOT, p)
        state = storage.load_state(path)
        stage = state["current_stage"]
        stage_name = storage.STAGE_NAMES_RU.get(stage, stage)
        lines.append(f"• {p} — {stage_name}")

    await message.reply("\n".join(lines))


@router_tg.message(Command("continue"))
async def cmd_continue(message: Message):
    if not is_allowed(message):
        return
    project_name = get_active_project(message)
    if not project_name:
        await message.reply("Сначала выбери проект: /use <имя> или /new <имя>")
        return

    project_path = storage.get_project_path(PROJECTS_ROOT, project_name)
    state = storage.load_state(project_path)
    stage = state["current_stage"]

    await enter_stage(message, project_path, stage)


@router_tg.message(Command("status"))
async def cmd_status(message: Message):
    if not is_allowed(message):
        return
    project_name = get_active_project(message)
    if not project_name:
        await message.reply("Нет активного проекта. /projects или /new <имя>")
        return

    project_path = storage.get_project_path(PROJECTS_ROOT, project_name)
    state = storage.load_state(project_path)
    stage = state["current_stage"]
    stage_name = storage.STAGE_NAMES_RU.get(stage, stage)

    await message.reply(format_status(project_name, stage, stage_name))


@router_tg.message(Command("export"))
async def cmd_export(message: Message):
    if not is_allowed(message):
        return
    project_name = get_active_project(message)
    if not project_name:
        await message.reply("Нет активного проекта.")
        return

    project_path = storage.get_project_path(PROJECTS_ROOT, project_name)
    files_sent = 0

    for fname in sorted(os.listdir(project_path)):
        fpath = os.path.join(project_path, fname)
        if fname.endswith(".md") and os.path.isfile(fpath):
            await message.answer_document(FSInputFile(fpath))
            files_sent += 1

    # Also check output/
    output_dir = os.path.join(project_path, "output")
    if os.path.exists(output_dir):
        for fname in sorted(os.listdir(output_dir)):
            fpath = os.path.join(output_dir, fname)
            if os.path.isfile(fpath):
                await message.answer_document(FSInputFile(fpath))
                files_sent += 1

    if files_sent == 0:
        await message.reply("Артефактов пока нет.")
    else:
        await message.reply(f"📎 Отправлено файлов: {files_sent}")


@router_tg.message(Command("skip"))
async def cmd_skip(message: Message):
    if not is_allowed(message):
        return
    project_name = get_active_project(message)
    if not project_name:
        await message.reply("Нет активного проекта.")
        return

    project_path = storage.get_project_path(PROJECTS_ROOT, project_name)
    state = storage.load_state(project_path)
    stage = state["current_stage"]

    if not prompts.is_manual_stage(stage):
        await message.reply("Текущий этап не ручной. Используй /continue.")
        return

    nxt = storage.next_stage(stage)
    if not nxt:
        await message.reply("Это последний этап.")
        return

    state["current_stage"] = nxt
    storage.save_state(project_path, state)

    stage_name = storage.STAGE_NAMES_RU.get(nxt, nxt)
    await message.reply(f"⏭ Переход к этапу: {stage_name}")
    await enter_stage(message, project_path, nxt)


# ---------------------------------------------------------------------------
# Text message handler — conversation flow
# ---------------------------------------------------------------------------


@router_tg.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
    if not is_allowed(message):
        return

    async with get_semaphore(message.from_user.id):
        await _handle_text_inner(message)


async def _handle_text_inner(message: Message):
    # Groups: save to context. Reply only on @mention.
    if message.chat.type in ("group", "supergroup"):
        project_name = storage.get_linked_project(PROJECTS_ROOT, message.chat.id)
        if not project_name:
            return
        project_path = storage.get_project_path(PROJECTS_ROOT, project_name)
        username = message.from_user.username or message.from_user.first_name or "user"
        storage.append_group_message(project_path, username, message.text)
        logger.info("Group message saved (project=%s, user=%s)", project_name, username)

        # Reply only if bot is mentioned
        bot_info = await bot.get_me()
        mention = f"@{bot_info.username}"
        if mention.lower() not in message.text.lower():
            await message.reply(f"📝 Записано в group_context.md ({project_name})")
            return

        # Mentioned — give a lite reply
        text = message.text.replace(mention, "").strip()
        await message.reply("🤖 Думаю...")
        try:
            result = await telegram_reply(text, project_path, mode="chat")
            asyncio.create_task(_check_balance_warning())
            await send_long(message, result)
        except Exception as e:
            logger.error("telegram_reply failed: %s", e, exc_info=True)
            await message.reply(f"❌ Ошибка: {e}")
        return

    # Private chat
    ustate = get_ustate(message.from_user.id)

    # Handle pending actions (bot asked for project name)
    if ustate.get("pending_action"):
        action = ustate["pending_action"]
        ustate["pending_action"] = None
        if action == "new":
            await _do_new_project(message, message.text)
        elif action == "use":
            await _do_use_project(message, message.text)
        return

    if not ustate["active_project"]:
        await message.reply("Сначала выбери или создай проект.\n/projects или /new <имя>")
        return

    project_path = storage.get_project_path(PROJECTS_ROOT, ustate["active_project"])
    await message.reply("🤖 Думаю...")
    try:
        result = await telegram_reply(message.text, project_path, mode="chat")
        asyncio.create_task(_check_balance_warning())
        await send_long(message, result)
    except Exception as e:
        logger.error("telegram_reply failed: %s", e, exc_info=True)
        await message.reply(f"❌ Ошибка: {e}")


# ---------------------------------------------------------------------------
# Lite Telegram commands (single draft-model call)
# ---------------------------------------------------------------------------


async def _lite_command(message: Message, mode: str):
    if not is_allowed(message):
        return
    parts = message.text.split(maxsplit=1)
    user_text = parts[1].strip() if len(parts) > 1 else ""

    project_name = get_active_project(message)
    if not project_name:
        await message.reply("Нет активного проекта. /use <имя> или /new <имя>")
        return

    if not user_text:
        hints = {"hypothesize": "тему", "brainstorm": "задачу", "rate": "идею"}
        await message.reply(f"Укажи {hints.get(mode, 'запрос')}: /{mode} <текст>")
        return

    project_path = storage.get_project_path(PROJECTS_ROOT, project_name)
    await message.reply("🤖 Думаю...")
    async with get_semaphore(message.from_user.id):
        try:
            result = await telegram_reply(user_text, project_path, mode=mode)
            asyncio.create_task(_check_balance_warning())
            await send_long(message, result)
        except Exception as e:
            logger.error("%s failed: %s", mode, e, exc_info=True)
            await message.reply(f"❌ Ошибка: {e}")


@router_tg.message(Command("hypothesize"))
async def cmd_hypothesize(message: Message):
    await _lite_command(message, "hypothesize")


@router_tg.message(Command("brainstorm"))
async def cmd_brainstorm(message: Message):
    await _lite_command(message, "brainstorm")


@router_tg.message(Command("rate"))
async def cmd_rate(message: Message):
    await _lite_command(message, "rate")


# ---------------------------------------------------------------------------
# Balance warning
# ---------------------------------------------------------------------------

_balance_warned = False


async def _check_balance_warning():
    global _balance_warned
    try:
        data = await get_balance()
        d = data.get("data", {})
        limit = d.get("limit")
        usage = d.get("usage", 0)
        if limit is not None:
            remaining = limit - usage
            if remaining < 1.0 and not _balance_warned:
                _balance_warned = True
                await bot.send_message(
                    OWNER_ID,
                    f"⚠️ OpenRouter: остаток ${remaining:.4f} — меньше $1. Пополни баланс."
                )
            elif remaining >= 1.0:
                _balance_warned = False
    except Exception as e:
        logger.warning("Balance check failed: %s", e)


# ---------------------------------------------------------------------------
# Voice message handler
# ---------------------------------------------------------------------------


@router_tg.message(F.voice)
async def handle_voice(message: Message):
    if not is_allowed(message):
        return

    async with get_semaphore(message.from_user.id):
        await _handle_voice_inner(message)


async def _handle_voice_inner(message: Message):
    from core.transcriber import _transcribe_sem
    queued = _transcribe_sem._value == 0  # semaphore locked = someone else transcribing
    if queued:
        wait = await message.reply("🎙 Голосовое получено, жду очереди на распознавание...")
    else:
        wait = await message.reply("🎙 Распознаю голосовое...")
    try:
        file = await bot.get_file(message.voice.file_id)
        ogg_bytes = await bot.download_file(file.file_path)
        text = await transcribe_ogg(ogg_bytes.read())
        logger.info("Voice transcribed (user=%s, len=%d chars)", message.from_user.id, len(text or ""))
    except Exception as e:
        await wait.delete()
        logger.error("Voice transcription failed: %s", e)
        await message.reply(f"Не удалось распознать голосовое: {e}")
        return

    await wait.delete()

    if not text:
        await message.reply("Не удалось разобрать речь.")
        return

    await message.reply(f"🗣 Распознано: {text}")

    # Save to group context if in group
    if message.chat.type in ("group", "supergroup"):
        project_name = storage.get_linked_project(PROJECTS_ROOT, message.chat.id)
        if project_name:
            project_path = storage.get_project_path(PROJECTS_ROOT, project_name)
            username = message.from_user.username or message.from_user.first_name or "user"
            storage.append_group_message(project_path, f"{username} [голосовое]", text)
            logger.info("Voice message saved to group_context.md (project=%s, user=%s)", project_name, username)
            await message.reply(f"📝 Голосовое записано в group_context.md ({project_name})")
        return

    # In private: treat transcribed text as regular message input
    message.text = text
    await handle_text(message)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main():
    logger.info("Starting PMF Pipeline Bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
