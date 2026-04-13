"""PMF Pipeline Telegram Bot — aiogram 3.x."""

import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from pathlib import Path

import yaml
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.bot import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    FSInputFile, Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, BotCommand, BotCommandScopeAllPrivateChats,
)

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import prompts, storage
from core.balance_monitor import start_periodic_check
from core.logging_config import setup_logging
from core.router import get_balance, telegram_reply
from core.transcriber import transcribe_ogg
from core.processor import chunk_text, format_status, format_stage_intro

setup_logging()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = PROJECT_ROOT / "config.yaml"
PID_FILE = PROJECT_ROOT / "data" / "bot.pid"
HEARTBEAT_FILE = PROJECT_ROOT / "data" / "bot.heartbeat"
with open(CONFIG_PATH, encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

OWNER_ID = cfg["owner_id"]
PROJECTS_ROOT = str(PROJECT_ROOT / cfg["projects_root"])
os.makedirs(PROJECTS_ROOT, exist_ok=True)
FEEDBACK_FILE = PROJECT_ROOT / "data" / "bot_feedback.md"
FEEDBACK_FILE.parent.mkdir(exist_ok=True)
USER_SESSION_FILE = PROJECT_ROOT / "data" / "user_sessions.json"
INVITE_TOKENS_FILE = PROJECT_ROOT / "data" / "invite_tokens.json"
GUEST_ACTIVITY_FILE = PROJECT_ROOT / "data" / "guest_activity.json"
_TG_CFG = cfg.get("telegram", {})
_MAX_RESPONSE_CHARS = _TG_CFG.get("max_response_chars", 2900)
_GROUP_CFG = cfg.get("group", {})
_BOT_UNAVAILABLE_TEXT = "❌ Внутренняя ошибка. Попробуй позже."


def _check_single_instance():
    data_dir = PID_FILE.parent
    data_dir.mkdir(exist_ok=True)

    if PID_FILE.exists():
        try:
            existing_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except (TypeError, ValueError):
            logger.warning("Invalid PID file content in %s; replacing it", PID_FILE)
        else:
            try:
                os.kill(existing_pid, 0)
            except OSError:
                logger.info("Found stale PID file for dead process %s; continuing", existing_pid)
            else:
                heartbeat_ts = None
                if HEARTBEAT_FILE.exists():
                    try:
                        heartbeat_ts = HEARTBEAT_FILE.stat().st_mtime
                    except OSError:
                        heartbeat_ts = None

                now = time.time()
                if heartbeat_ts is not None and now - heartbeat_ts < 1800:
                    logger.warning(
                        "Another bot instance is active (pid=%s, heartbeat age=%.0fs); exiting",
                        existing_pid,
                        now - heartbeat_ts,
                    )
                    sys.exit(0)

                logger.info(
                    "Existing bot instance appears frozen (pid=%s, heartbeat_age=%s); sending SIGTERM",
                    existing_pid,
                    "unknown" if heartbeat_ts is None else f"{now - heartbeat_ts:.0f}s",
                )
                try:
                    os.kill(existing_pid, signal.SIGTERM)
                except OSError as exc:
                    logger.info("Failed to terminate stale bot process %s: %s", existing_pid, exc)

    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


async def _heartbeat_loop():
    HEARTBEAT_FILE.parent.mkdir(exist_ok=True)
    while True:
        HEARTBEAT_FILE.write_text(str(time.time()), encoding="utf-8")
        await asyncio.sleep(1800)


def _cleanup_instance_files():
    for path in (PID_FILE, HEARTBEAT_FILE):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Failed to remove %s: %s", path, exc)

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

bot = Bot(token=cfg["bot"]["token"], default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()
router_tg = Router()
dp.include_router(router_tg)

# ---------------------------------------------------------------------------
# Per-user in-memory state
# ---------------------------------------------------------------------------

user_state: dict[int, dict] = {}
user_semaphores: dict[int, asyncio.Semaphore] = {}


def _load_sessions() -> dict[int, str]:
    """Load {user_id: active_project} from disk."""
    if USER_SESSION_FILE.exists():
        try:
            raw = json.loads(USER_SESSION_FILE.read_text(encoding="utf-8"))
            return {int(k): v for k, v in raw.items()}
        except Exception:
            pass
    return {}


def _save_session(user_id: int, project: str | None):
    sessions = _load_sessions()
    if project:
        sessions[user_id] = project
    else:
        sessions.pop(user_id, None)
    USER_SESSION_FILE.write_text(
        json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_invite_tokens() -> dict:
    if INVITE_TOKENS_FILE.exists():
        return json.loads(INVITE_TOKENS_FILE.read_text(encoding="utf-8"))
    return {}


def _save_invite_tokens(tokens: dict):
    INVITE_TOKENS_FILE.parent.mkdir(exist_ok=True)
    INVITE_TOKENS_FILE.write_text(
        json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _get_guest_project(user_id: int) -> str | None:
    """Return the project name if user_id is a registered guest in any project."""
    projects_root = Path(PROJECTS_ROOT)
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        guests_file = project_dir / "guests.json"
        if guests_file.exists():
            guests = json.loads(guests_file.read_text(encoding="utf-8"))
            if str(user_id) in guests:
                return project_dir.name
    return None


def _log_guest_activity(user_id: int, username: str, project: str, event: str, detail: str):
    from datetime import datetime

    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "user_id": user_id,
        "username": username,
        "project": project,
        "event": event,
        "detail": detail,
    }
    GUEST_ACTIVITY_FILE.parent.mkdir(exist_ok=True)
    existing = (
        json.loads(GUEST_ACTIVITY_FILE.read_text(encoding="utf-8"))
        if GUEST_ACTIVITY_FILE.exists() else []
    )
    existing.append(entry)
    GUEST_ACTIVITY_FILE.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _guest_username(message: Message) -> str:
    u = message.from_user
    return " ".join(filter(None, [u.first_name, u.last_name])) or u.username or str(u.id)


def _guest_detail(text: str, limit: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"

# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

# Unsupported natural-language requests — bot admits limitations and suggests commands
_UNSUPPORTED_INTENTS = [
    ("запусти пайплайн", [
        "Я пока не умею запускать пайплайн по текстовой команде 🙁",
        "Используй: `/continue` — начать/продолжить текущий этап",
    ]),
    ("начни этап", [
        "Я пока не могу начать этап по текстовому запросу 🙁",
        "Используй: `/continue` — начать текущий этап",
    ]),
    ("начни с нуля", [
        "Я пока не умею стартовать пайплайн с нуля по тексту 🙁",
        "Используй: `/new <имя>` — создать проект и начать с 0_setup",
    ]),
    ("создай проект", [
        "Я пока не могу создать проект по текстовому описанию 🙁",
        "Используй: `/new <имя>` — создать новый проект",
    ]),
    ("проведи интервью", [
        "Я пока не провожу интервью автоматически 🙁",
        "Это ручной этап. Проведи 15-20 встреч, сохрани заметки, потом жми `/skip`",
    ]),
    ("запусти мвп", [
        "Запуск MVP — это твоя задача, я тут не помогу 🙁",
        "Когда будут данные (≈40 пользователей) — жми `/skip` для перехода к метрикам",
    ]),
    ("сделай исследование", [
        "Я пока не могу сам провести исследование рынка 🙁",
        "Но могу помочь на этапе `/continue` — отвечу на вопросы по контексту проекта",
    ]),
]

def detect_unsupported(text: str) -> list[str] | None:
    """If user asks for something the bot can't do naturally — admit it and suggest a command."""
    lower = text.lower()
    for keyword, replies in _UNSUPPORTED_INTENTS:
        if keyword in lower:
            return replies
    return None

_INTENTS = [
    ("hypothesize", "💡 Сформировать гипотезы", [
        "гипотез", "предположен", "допущен", "hypothesis",
    ]),
    ("brainstorm", "🧠 Провести брейнсторм", [
        "брейнсторм", "мозговой штурм", "идеи", "brainstorm", "накидай",
    ]),
    ("rate", "⭐ Оценить идею", [
        "оцени", "оценку дай", "насколько хорош", "rate", "рейтинг идеи",
    ]),
]

def detect_intent(text: str) -> tuple[str, str] | None:
    """Return (mode, label) if text matches a known intent, else None."""
    lower = text.lower()
    for mode, label, keywords in _INTENTS:
        if any(kw in lower for kw in keywords):
            return mode, label
    return None

# Pending intent storage: callback_id → {text, project_path, username, mode, label, created_at}
_pending_intents: dict[str, dict] = {}
_PENDING_INTENT_TTL = 300  # 5 minutes


def _cleanup_stale_intents():
    """Remove pending intents older than TTL seconds."""
    now = time.time()
    stale = [cid for cid, data in _pending_intents.items() if now - data.get("created_at", 0) > _PENDING_INTENT_TTL]
    for cid in stale:
        del _pending_intents[cid]
    if stale:
        logger.info("Cleaned up %d stale pending intents", len(stale))


def get_ustate(user_id: int) -> dict:
    if user_id not in user_state:
        sessions = _load_sessions()
        user_state[user_id] = {
            "active_project": sessions.get(user_id),
            "question_index": 0,
            "answers": [],
            "pending_action": None,
        }
    return user_state[user_id]


def get_semaphore(user_id: int) -> asyncio.Semaphore:
    if user_id not in user_semaphores:
        user_semaphores[user_id] = asyncio.Semaphore(1)
    return user_semaphores[user_id]


def _session_menu(project_name: str) -> ReplyKeyboardMarkup:
    """Persistent bottom menu showing current project context."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=f"📁 {project_name}")],
            [KeyboardButton(text="🔄 Сменить проект"), KeyboardButton(text="➕ Новый проект")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Пиши вопрос по проекту...",
    )


def _guest_menu(project_name: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=f"📁 {project_name}")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Пиши вопрос по проекту...",
    )


def _project_keyboard(projects: list[str]) -> InlineKeyboardMarkup:
    """Inline keyboard: one button per project + New + Cancel."""
    rows = [[InlineKeyboardButton(text=f"📁 {p}", callback_data=f"proj:use:{p}")] for p in projects]
    rows.append([
        InlineKeyboardButton(text="➕ Новый проект", callback_data="proj:new"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="proj:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def is_allowed(message: Message) -> bool:
    """Allow anyone in group chats; restrict private chats to owner only."""
    if message.chat.type in ("group", "supergroup"):
        return cfg.get("group", {}).get("allowed_in_groups", True)
    return message.from_user.id == OWNER_ID or _get_guest_project(message.from_user.id) is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_active_project(message: Message) -> str | None:
    """Return active project: group-linked project or user's personal active project."""
    if message.chat.type in ("group", "supergroup"):
        return storage.get_linked_project(PROJECTS_ROOT, message.chat.id)
    guest_project = _get_guest_project(message.from_user.id)
    if guest_project:
        return guest_project
    return get_ustate(message.from_user.id)["active_project"]


async def send_long(message: Message, text: str):
    """Send text split into Telegram-safe chunks. First chunk replies to original message."""
    chunks = chunk_text(text, max_len=_MAX_RESPONSE_CHARS)
    for i, chunk in enumerate(chunks):
        if i == 0:
            await message.reply(chunk)
        else:
            await message.answer(chunk)


def _sanitize_error_message(exc: Exception) -> str:
    token = cfg.get("bot", {}).get("token")
    text = str(exc).strip() or exc.__class__.__name__
    if token:
        text = text.replace(token, "***")
    return text[:300]


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
    from datetime import datetime

    parts = (message.text or "").split(maxsplit=1)
    start_arg = parts[1].strip() if len(parts) > 1 else ""

    if start_arg.startswith("inv_"):
        tokens = _load_invite_tokens()
        token_data = tokens.get(start_arg)
        if not token_data:
            await message.reply("❌ Приглашение не найдено.")
            return
        if token_data.get("used"):
            await message.reply("❌ Это приглашение уже использовано.")
            return
        if datetime.fromisoformat(token_data["expires_at"]) < datetime.now():
            await message.reply("❌ Срок действия приглашения истёк.")
            return

        project_name = token_data["project"]
        project_path = Path(PROJECTS_ROOT) / project_name
        if not project_path.exists():
            await message.reply(f"❌ Проект «{project_name}» не найден.")
            return

        guests_file = project_path / "guests.json"
        guests = json.loads(guests_file.read_text(encoding="utf-8")) if guests_file.exists() else {}
        username = _guest_username(message)
        guests[str(message.from_user.id)] = {
            "username": username,
            "added_at": datetime.now().isoformat(timespec="seconds"),
            "invited_via": start_arg,
        }
        guests_file.write_text(json.dumps(guests, ensure_ascii=False, indent=2), encoding="utf-8")

        token_data["used"] = True
        token_data["used_by"] = str(message.from_user.id)
        tokens[start_arg] = token_data
        _save_invite_tokens(tokens)
        _log_guest_activity(
            message.from_user.id,
            username,
            project_name,
            "joined",
            f"Принял приглашение {start_arg}",
        )
        await message.reply(
            f"👋 Добро пожаловать в проект «{project_name}».\n"
            "Теперь можешь задавать вопросы по этому проекту.",
            reply_markup=_guest_menu(project_name),
        )
        return

    if not is_allowed(message):
        await message.reply("❌ Нет доступа. Нужна ссылка-приглашение от владельца.")
        return
    guest_project = _get_guest_project(message.from_user.id)
    if guest_project:
        await message.reply(
            f"👋 Ты подключён к проекту «{guest_project}».\nПиши вопрос по контексту проекта.",
            reply_markup=_guest_menu(guest_project),
        )
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
    _save_session(message.from_user.id, name)

    is_private = message.chat.type == "private"
    menu = _session_menu(name) if is_private else None
    await message.reply(f"✅ Проект '{name}' создан!", reply_markup=menu)

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
        limit_value = float(limit) if limit is not None else None
        usage = float(d.get("usage", 0))
        remaining = (limit_value - usage) if limit_value is not None else None

        lines = ["💳 OpenRouter баланс\n"]
        if limit_value is not None:
            lines.append(f"Лимит: ${limit_value:.4f}")
            lines.append(f"Использовано: ${usage:.4f}")
            lines.append(f"Остаток: ${remaining:.4f}")
        else:
            lines.append(f"Использовано: ${usage:.4f}")
            lines.append("Лимит: не установлен (pay-as-you-go)")
        await message.reply("\n".join(lines))
    except Exception as exc:
        logger.error("cmd_balance failed: %s", _sanitize_error_message(exc))
        await message.reply(_BOT_UNAVAILABLE_TEXT)


@router_tg.message(Command("share"))
async def cmd_share(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Использование: /share <название_проекта>")
        return
    project_name = parts[1].strip()
    project_path = Path(PROJECTS_ROOT) / project_name
    if not project_path.exists():
        await message.reply(f"❌ Проект «{project_name}» не найден.")
        return
    import secrets
    from datetime import datetime, timedelta

    token = "inv_" + secrets.token_urlsafe(8)
    tokens = _load_invite_tokens()
    tokens[token] = {
        "project": project_name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "expires_at": (datetime.now() + timedelta(hours=48)).isoformat(timespec="seconds"),
        "used": False,
        "used_by": None,
    }
    _save_invite_tokens(tokens)
    bot_info = await message.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={token}"
    await message.reply(
        f"🔗 Ссылка для приглашения в проект «{project_name}»:\n{link}\n\n"
        "⏳ Действует 48 часов, одноразовая."
    )


@router_tg.message(Command("revoke"))
async def cmd_revoke(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Использование: /revoke <user_id>")
        return
    target_id = parts[1].strip()
    removed_from = []
    for project_dir in Path(PROJECTS_ROOT).iterdir():
        if not project_dir.is_dir():
            continue
        guests_file = project_dir / "guests.json"
        if guests_file.exists():
            guests = json.loads(guests_file.read_text(encoding="utf-8"))
            if target_id in guests:
                del guests[target_id]
                guests_file.write_text(json.dumps(guests, ensure_ascii=False, indent=2), encoding="utf-8")
                removed_from.append(project_dir.name)
    if removed_from:
        await message.reply(f"✅ Доступ отозван. Проекты: {', '.join(removed_from)}")
    else:
        await message.reply(f"⚠️ Пользователь {target_id} не найден среди гостей.")


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
    if message.chat.type == "private" and _get_guest_project(message.from_user.id):
        await message.reply("❌ Гостевой доступ не позволяет создавать проекты.")
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
    _save_session(message.from_user.id, name)

    project_path = storage.get_project_path(PROJECTS_ROOT, name)
    state = storage.load_state(project_path)
    stage = state["current_stage"]
    stage_name = storage.STAGE_NAMES_RU.get(stage, stage)

    is_private = message.chat.type == "private"
    menu = _session_menu(name) if is_private else None
    await message.reply(format_status(name, stage, stage_name), reply_markup=menu)
    if is_private:
        await message.reply("Пиши вопросы — я рядом. /continue чтобы продолжить этап.")


@router_tg.message(Command("use"))
async def cmd_use(message: Message):
    if not is_allowed(message):
        return
    if message.chat.type == "private" and _get_guest_project(message.from_user.id):
        await message.reply("❌ У тебя фиксированный гостевой доступ к одному проекту.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        existing = storage.list_projects(PROJECTS_ROOT)
        if existing:
            await message.reply("Выбери проект:", reply_markup=_project_keyboard(existing))
        else:
            await message.reply("Нет проектов.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="➕ Создать проект", callback_data="proj:new"),
            ]]))
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
# Feedback command — bot complaints log
# ---------------------------------------------------------------------------


@router_tg.message(Command("feedback"))
async def cmd_feedback(message: Message):
    from datetime import datetime
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "Опиши что не так:\n/feedback <текст>\n\nНапример: /feedback бот ответил не на тот вопрос"
        )
        return

    text = parts[1].strip()
    u = message.from_user
    username = f"{u.first_name or ''} {u.last_name or ''}".strip() or u.username or str(u.id)
    chat_type = message.chat.type

    # Capture current context
    context_lines = []
    if chat_type == "private":
        ustate = get_ustate(u.id)
        project = ustate.get("active_project")
        if project:
            project_path = storage.get_project_path(PROJECTS_ROOT, project)
            state = storage.load_state(project_path)
            context_lines.append(f"Проект: {project}")
            context_lines.append(f"Этап: {state.get('current_stage', '?')}")
    elif chat_type in ("group", "supergroup"):
        project = storage.get_linked_project(PROJECTS_ROOT, message.chat.id)
        if project:
            context_lines.append(f"Проект: {project}")
            context_lines.append(f"Группа: {message.chat.title or message.chat.id}")

    context_str = "\n".join(context_lines) if context_lines else "нет контекста"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    source_label = "📱 личка" if chat_type == "private" else "👥 группа"

    entry = (
        f"## [{ts}] | {source_label} | {username}\n"
        f"**Контекст:** {context_str}\n"
        f"**Замечание:** {text}\n"
        f"**Решение:** —\n\n"
        f"---\n\n"
    )

    existing = FEEDBACK_FILE.read_text(encoding="utf-8") if FEEDBACK_FILE.exists() else ""
    FEEDBACK_FILE.write_text(entry + existing, encoding="utf-8")
    logger.info("Feedback saved from user=%s", u.id)

    await message.reply("✅ Записал. Разберёмся.")


# ---------------------------------------------------------------------------
# Text message handler — conversation flow
# ---------------------------------------------------------------------------


@router_tg.message(F.text == "🔄 Сменить проект")
async def menu_switch_project(message: Message):
    if message.chat.type != "private":
        return
    if _get_guest_project(message.from_user.id):
        return
    existing = storage.list_projects(PROJECTS_ROOT)
    await message.reply("Выбери проект:", reply_markup=_project_keyboard(existing))


@router_tg.message(F.text == "➕ Новый проект")
async def menu_new_project(message: Message):
    if message.chat.type != "private":
        return
    if _get_guest_project(message.from_user.id):
        return
    ustate = get_ustate(message.from_user.id)
    ustate["pending_action"] = "new"
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="proj:cancel"),
    ]])
    await message.reply("Введи название нового проекта:", reply_markup=cancel_kb)


@router_tg.message(F.text.startswith("📁 "))
async def menu_project_status(message: Message):
    if message.chat.type != "private":
        return
    project_name = get_active_project(message)
    if not project_name:
        return
    project_path = storage.get_project_path(PROJECTS_ROOT, project_name)
    state = storage.load_state(project_path)
    stage = state["current_stage"]
    stage_name = storage.STAGE_NAMES_RU.get(stage, stage)
    await message.reply(format_status(project_name, stage, stage_name))


@router_tg.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
    logger.info(
        "handle_text: chat_id=%s type=%s user=%s text=%r",
        message.chat.id, message.chat.type,
        getattr(message.from_user, "id", None),
        (message.text or "")[:80],
    )
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
        u = message.from_user
        full_name = " ".join(filter(None, [u.first_name, u.last_name])) or u.username or "user"
        username = storage.get_user_display_name(project_path, u.id, fallback=full_name)
        await asyncio.to_thread(storage.append_group_message, project_path, username, message.text)
        logger.info("Group message saved (project=%s, user=%s)", project_name, username)

        # Reply only if bot is mentioned
        bot_info = await bot.get_me()
        mention = f"@{bot_info.username}"
        trigger_mode = _GROUP_CFG.get("trigger_mode", "mention")
        keywords = _GROUP_CFG.get("keywords", [])
        should_reply = mention.lower() in message.text.lower()
        if not should_reply and trigger_mode == "keywords":
            should_reply = any(kw.lower() in message.text.lower() for kw in keywords)
        if not should_reply:
            return

        # Mentioned — detect intent first, then unsupported as fallback
        text = message.text.replace(mention, "").strip()
        intent = detect_intent(text)
        if intent:
            mode, label = intent
            cid = str(uuid.uuid4())[:8]
            _pending_intents[cid] = {
                "text": text,
                "project_path": project_path,
                "username": username,
                "mode": mode,
                "created_at": time.time(),
            }
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=label, callback_data=f"intent:{cid}:do"),
                InlineKeyboardButton(text="💬 Другое", callback_data=f"intent:{cid}:chat"),
            ]])
            await message.reply(
                f"Похоже, ты хочешь: {label}?\nВыбери или уточни что нужно:",
                reply_markup=kb,
            )
            return

        # No intent — check unsupported as fallback, then give a lite reply
        unsupported = detect_unsupported(text)
        if unsupported:
            reply = "\n".join(unsupported)
            await message.reply(reply)
            return

        await message.reply("🤖 Думаю...")
        try:
            result = await telegram_reply(text, project_path, mode="chat", username=username)
            asyncio.create_task(_check_balance_warning())
            logger.info("send_long: result len=%s, preview=%r", len(result) if result else None, (result or "")[:80])
            await send_long(message, result)
            logger.info("send_long: done")
        except Exception as exc:
            logger.error("group reply failed: %s | %s", exc.__class__.__name__, _sanitize_error_message(exc))
            try:
                await message.reply(_BOT_UNAVAILABLE_TEXT)
            except Exception as e2:
                logger.error("fallback reply also failed: %s", e2)
        return

    # Private chat
    guest_project = _get_guest_project(message.from_user.id)
    if guest_project:
        project_path = storage.get_project_path(PROJECTS_ROOT, guest_project)
        username = _guest_username(message)

        # No intent — check unsupported as fallback
        unsupported = detect_unsupported(message.text)
        if unsupported:
            reply = "\n".join(unsupported)
            await message.reply(reply, reply_markup=_guest_menu(guest_project))
            return

        intent = detect_intent(message.text)
        if intent:
            mode, label = intent

            cid = str(uuid.uuid4())[:8]
            _pending_intents[cid] = {
                "text": message.text,
                "project_path": project_path,
                "username": username,
                "mode": mode,
                "project_name": guest_project,
                "user_id": message.from_user.id,
                "is_guest": True,
                "created_at": time.time(),
            }
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=label, callback_data=f"intent:{cid}:do"),
                InlineKeyboardButton(text="💬 Другое", callback_data=f"intent:{cid}:chat"),
            ]])
            await message.reply(
                f"Похоже, ты хочешь: {label}?\nВыбери или уточни что нужно:",
                reply_markup=kb,
            )
            return

        await message.reply("🤖 Думаю...", reply_markup=_guest_menu(guest_project))
        try:
            result = await telegram_reply(message.text, project_path, mode="chat", username=username)
            asyncio.create_task(_check_balance_warning())
            await send_long(message, result)
            _log_guest_activity(
                message.from_user.id,
                username,
                guest_project,
                "chat",
                _guest_detail(message.text),
            )
        except Exception as exc:
            logger.error("telegram_reply failed for guest: %s", _sanitize_error_message(exc))
            await message.reply(_BOT_UNAVAILABLE_TEXT)
        return

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

    # Check unsupported as fallback (after intent check above)
    unsupported = detect_unsupported(message.text)
    if unsupported:
        reply = "\n".join(unsupported)
        await message.reply(reply)
        return

    if not ustate["active_project"]:
        existing = storage.list_projects(PROJECTS_ROOT)
        await message.reply("Сначала выбери проект:", reply_markup=_project_keyboard(existing))
        return

    project_path = storage.get_project_path(PROJECTS_ROOT, ustate["active_project"])
    await message.reply("🤖 Думаю...")
    try:
        result = await telegram_reply(message.text, project_path, mode="chat")
        asyncio.create_task(_check_balance_warning())
        await send_long(message, result)
    except Exception as exc:
        logger.error("telegram_reply failed: %s", _sanitize_error_message(exc))
        await message.reply(_BOT_UNAVAILABLE_TEXT)


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
            username = _guest_username(message) if message.chat.type == "private" and _get_guest_project(message.from_user.id) else None
            result = await telegram_reply(user_text, project_path, mode=mode, username=username)
            asyncio.create_task(_check_balance_warning())
            await send_long(message, result)
            guest_project = _get_guest_project(message.from_user.id) if message.chat.type == "private" else None
            if guest_project:
                _log_guest_activity(
                    message.from_user.id,
                    username or str(message.from_user.id),
                    guest_project,
                    mode,
                    _guest_detail(user_text),
                )
        except Exception as exc:
            logger.error("%s failed: %s", mode, _sanitize_error_message(exc))
            await message.reply(_BOT_UNAVAILABLE_TEXT)


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
        limit_value = float(limit) if limit is not None else None
        usage = float(d.get("usage", 0))
        if limit_value is not None:
            remaining = limit_value - usage
            if remaining < 1.0 and not _balance_warned:
                _balance_warned = True
                await bot.send_message(
                    OWNER_ID,
                    f"⚠️ OpenRouter: остаток ${remaining:.4f} — меньше $1. Пополни баланс."
                )
            elif remaining >= 1.0:
                _balance_warned = False
    except Exception as exc:
        logger.warning("Balance check failed: %s", _sanitize_error_message(exc))


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
    except Exception as exc:
        await wait.delete()
        logger.error("Voice transcription failed: %s", _sanitize_error_message(exc))
        await message.reply("Не удалось распознать голосовое.")
        return

    await wait.delete()

    if not text:
        await message.reply("Не удалось разобрать речь.")
        return

    # Show transcript first
    await message.reply(f"🗣 Распознано: {text}")

    # Post-process: summarize + extract actions
    await message.reply("🧠 Анализирую голосовое...")
    try:
        from core.router import summarize_voice_message
        project_name = None
        if message.chat.type in ("group", "supergroup"):
            project_name = storage.get_linked_project(PROJECTS_ROOT, message.chat.id)
        else:
            guest_project = _get_guest_project(message.from_user.id)
            if guest_project:
                project_name = guest_project
            else:
                ustate = get_ustate(message.from_user.id)
                project_name = ustate.get("active_project")

        if project_name:
            project_path = storage.get_project_path(PROJECTS_ROOT, project_name)
            summary = await summarize_voice_message(text, project_path)
            await message.reply(f"📋 Анализ голосового для проекта «{project_name}»:\n\n{summary}")
        else:
            await message.reply("💡 Выбери проект, чтобы я мог проанализировать голосовое в контексте.")
    except Exception as exc:
        logger.error("Voice post-processing failed: %s", _sanitize_error_message(exc))
        await message.reply("Не удалось проанализировать голосовое. Попробуй ещё раз.")

    # Also save to group context if in group
    if message.chat.type in ("group", "supergroup"):
        project_name = storage.get_linked_project(PROJECTS_ROOT, message.chat.id)
        if project_name:
            project_path = storage.get_project_path(PROJECTS_ROOT, project_name)
            u = message.from_user
            full_name = " ".join(filter(None, [u.first_name, u.last_name])) or u.username or "user"
            display = storage.get_user_display_name(project_path, u.id, fallback=full_name)
            await asyncio.to_thread(storage.append_group_message, project_path, f"{display} [голосовое]", text)
            logger.info("Voice message saved to group_context.md (project=%s, user=%s)", project_name, display)
        return  # done — already sent transcript + analysis

    # In private: just log activity, no extra LLM reply (transcript + analysis is enough)
    guest_project = _get_guest_project(message.from_user.id)
    if guest_project:
        _log_guest_activity(
            message.from_user.id,
            _guest_username(message),
            guest_project,
            "voice",
            _guest_detail(text),
        )
        return

    ustate = get_ustate(message.from_user.id)
    if ustate.get("active_project"):
        logger.info("Voice message processed for project=%s (user=%s)", ustate["active_project"], message.from_user.id)


# ---------------------------------------------------------------------------
# Project selection callback handler (private chat)
# ---------------------------------------------------------------------------


@router_tg.callback_query(F.data.startswith("proj:"))
async def handle_proj_callback(callback: CallbackQuery):
    async with get_semaphore(callback.from_user.id):
        await _handle_proj_callback_inner(callback)


async def _handle_proj_callback_inner(callback: CallbackQuery):
    if _get_guest_project(callback.from_user.id):
        await callback.answer("Гостевой доступ закреплён за одним проектом.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    action = parts[1]

    await callback.message.edit_reply_markup(reply_markup=None)

    if action == "cancel":
        ustate = get_ustate(callback.from_user.id)
        ustate["pending_action"] = None
        await callback.answer("Отменено.")
        await callback.message.answer("Хорошо, отменено. Напиши что нужно.")

    elif action == "use":
        project_name = parts[2]
        await callback.answer()
        ustate = get_ustate(callback.from_user.id)
        ustate["active_project"] = project_name
        _save_session(callback.from_user.id, project_name)
        project_path = storage.get_project_path(PROJECTS_ROOT, project_name)
        state = storage.load_state(project_path)
        stage = state["current_stage"]
        stage_name = storage.STAGE_NAMES_RU.get(stage, stage)
        menu = _session_menu(project_name)
        await callback.message.answer(format_status(project_name, stage, stage_name), reply_markup=menu)
        await callback.message.answer("Пиши вопросы — я рядом. /continue чтобы продолжить этап.")

    elif action == "new":
        await callback.answer()
        ustate = get_ustate(callback.from_user.id)
        ustate["pending_action"] = "new"
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="proj:cancel"),
        ]])
        await callback.message.reply("Введи название нового проекта:", reply_markup=cancel_kb)


# ---------------------------------------------------------------------------
# Intent callback handler
# ---------------------------------------------------------------------------


@router_tg.callback_query(F.data.startswith("intent:"))
async def handle_intent_callback(callback: CallbackQuery):
    async with get_semaphore(callback.from_user.id):
        await _handle_intent_callback_inner(callback)


async def _handle_intent_callback_inner(callback: CallbackQuery):
    _cleanup_stale_intents()  # clean up on each callback invocation
    parts = callback.data.split(":")
    cid, action = parts[1], parts[2]
    pending = _pending_intents.pop(cid, None)

    if not pending:
        await callback.answer("Запрос устарел, напиши заново.", show_alert=True)
        return

    await callback.message.edit_reply_markup(reply_markup=None)

    if action == "do":
        await callback.answer()
        await callback.message.answer("🤖 Думаю...")
        try:
            result = await telegram_reply(
                pending["text"],
                pending["project_path"],
                mode=pending["mode"],
                username=pending["username"],
            )
            asyncio.create_task(_check_balance_warning())
            await send_long(callback.message, result)
            if pending.get("is_guest"):
                _log_guest_activity(
                    pending["user_id"],
                    pending["username"],
                    pending["project_name"],
                    pending["mode"],
                    _guest_detail(pending["text"]),
                )
            _artifact_dirs = {"hypothesize": "hypotheses", "brainstorm": "brainstorm", "rate": "ratings"}
            if pending["mode"] in _artifact_dirs:
                await callback.message.answer(f"✅ Сохранено в проект: `{_artifact_dirs[pending['mode']]}/`")
        except Exception as exc:
            logger.error("intent callback failed: %s", _sanitize_error_message(exc))
            await callback.message.answer(_BOT_UNAVAILABLE_TEXT)

    elif action == "chat":
        await callback.answer()
        await callback.message.answer("Хорошо, напиши что именно тебе нужно 👇")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _register_commands():
    """Register slash commands so they appear in Telegram's '/' menu."""
    private_commands = [
        BotCommand(command="hypothesize", description="Сгенерировать гипотезы"),
        BotCommand(command="brainstorm", description="Мозговой штурм"),
        BotCommand(command="rate", description="Оценить идею"),
        BotCommand(command="projects", description="Список проектов"),
        BotCommand(command="use", description="Выбрать проект"),
        BotCommand(command="new", description="Создать новый проект"),
        BotCommand(command="status", description="Статус текущего проекта"),
        BotCommand(command="export", description="Экспорт артефактов"),
        BotCommand(command="balance", description="Баланс OpenRouter"),
        BotCommand(command="feedback", description="Оставить замечание по боту"),
    ]
    await bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
    logger.info("Bot commands registered.")


async def main():
    logger.info("Starting PMF Pipeline Bot...")
    _check_single_instance()
    await _register_commands()

    periodic_task = asyncio.create_task(start_periodic_check(bot, OWNER_ID))
    heartbeat_task = asyncio.create_task(_heartbeat_loop())

    try:
        await dp.start_polling(bot)
    finally:
        for task in (heartbeat_task, periodic_task):
            task.cancel()
        await asyncio.gather(heartbeat_task, periodic_task, return_exceptions=True)
        _cleanup_instance_files()


if __name__ == "__main__":
    asyncio.run(main())
