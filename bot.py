"""Main Telegram bot entrypoint and command routing."""

from __future__ import annotations

import asyncio
import io
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiofiles
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from admin import AdminHandlers, register_admin_handlers
from ai_engine import AIEngine, AIEngineError
from config_manager import ConfigManager
from docker_runner import DockerRunner
from file_manager import FileManager
from memory import MemoryManager
from middleware import MiddlewareService
from security import SecurityError, SecurityManager
from utils import (
    build_unicode_progress_bar,
    clamp_text,
    format_elapsed,
    format_stats_message,
    get_system_stats,
    is_truthy_env,
    normalize_whitespace,
    safe_markdown,
    setup_logging,
    should_trigger_continue,
    utc_now_iso,
)

SESSION_PENDING_KEY = "pending_generation"
SESSION_STATUS_KEY = "status_panel"
SESSION_RENAME_CHAT_KEY = "pending_rename_chat_id"
STATUS_EDIT_MIN_INTERVAL_SECONDS = 1.2
DEFAULT_STATUS_TEXT = "Idle and ready"
PROMPT_ENHANCEMENT_DEBUG_ENV = "AIAGENT_DEBUG_PROMPT_ENHANCEMENT"


class BotServices:
    """Container object for dependency wiring."""

    def __init__(self, root_dir: str) -> None:
        self.root_dir = Path(root_dir)
        self.logger = setup_logging(str(self.root_dir / "logs"))
        self.config = ConfigManager(root_dir)
        self.memory = MemoryManager(root_dir)
        self.security = SecurityManager(
            os.getenv("APP_ENCRYPTION_SECRET") or os.getenv("TELEGRAM_BOT_TOKEN") or "fallback-seed"
        )
        self.files = FileManager(root_dir, self.security, max_file_size_mb=self.config.config.limits.max_file_size_mb)
        self.middleware = MiddlewareService(root_dir, self.config, self.memory)
        self.ai = AIEngine(self.config, self.memory)
        self.docker = DockerRunner(self.config, self.security)


def services_from_context(context: ContextTypes.DEFAULT_TYPE) -> BotServices:
    return context.application.bot_data["services"]


def _status_keyboard(include_continue: bool = True) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton("📊 STATUS", callback_data="cmd:status"), InlineKeyboardButton("💬 RECENT CHATS", callback_data="cmd:chats")]
    row2: list[InlineKeyboardButton] = [InlineKeyboardButton("📦 DOWNLOAD ZIP", callback_data="cmd:zip")]
    if include_continue:
        row2.append(InlineKeyboardButton("▶ CONTINUE", callback_data="cmd:continue"))
    row3 = [InlineKeyboardButton("⛔ STOP", callback_data="cmd:stop")]
    return InlineKeyboardMarkup([row1, row2, row3])


def _completion_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📦 DOWNLOAD ZIP", callback_data="cmd:zip"),
                InlineKeyboardButton("🔁 REGENERATE", callback_data="cmd:continue"),
            ],
            [InlineKeyboardButton("💬 OPEN CHAT", callback_data="cmd:chats")],
        ]
    )


def _chat_actions_keyboard(chats: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for chat in chats[:6]:
        cid = str(chat.get("id", ""))
        rows.append(
            [
                InlineKeyboardButton("📂 OPEN", callback_data=f"chat:open:{cid}"),
                InlineKeyboardButton("✏️ RENAME", callback_data=f"chat:rename:{cid}"),
                InlineKeyboardButton("🗑 DELETE", callback_data=f"chat:delete:{cid}"),
            ]
        )
    rows.append([InlineKeyboardButton("🆕 NEW CHAT", callback_data="cmd:newchat")])
    return InlineKeyboardMarkup(rows)


async def _load_session(services: BotServices, user_id: int) -> dict[str, Any]:
    return await services.memory.load_session(user_id)


async def _save_session(services: BotServices, user_id: int, session: dict[str, Any]) -> None:
    await services.memory.save_session(user_id, session)


def _extract_required_files(plan_text: str) -> list[str]:
    pattern = re.compile(r"(?m)^\s*(?:[-*]\s*|[0-9]+\.\s*)?([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)\s*$")
    seen: set[str] = set()
    files: list[str] = []
    for match in pattern.finditer(plan_text):
        rel = match.group(1).strip().lstrip("./")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        files.append(rel)
    return files


def _render_status_panel(
    status: str,
    current_task: str,
    progress_percent: int,
    files_done: int,
    files_total: int | None,
    elapsed_seconds: float,
    current_file: str = "-",
) -> str:
    files_total_text = str(files_total) if files_total is not None else "?"
    progress_bar = build_unicode_progress_bar(progress_percent, width=9, done_char="▓", remain_char="░")
    return (
        "🤖 AI Coding Agent\n\n"
        "Status:\n"
        f"{status}\n\n"
        "Current Task:\n"
        f"{current_task}\n\n"
        "Progress:\n"
        f"{progress_bar}\n\n"
        "Files:\n"
        f"{files_done}/{files_total_text} completed\n\n"
        "Current File:\n"
        f"{current_file}\n\n"
        "Elapsed:\n"
        f"{format_elapsed(elapsed_seconds)}"
    )


async def _upsert_status_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    status: str,
    task: str,
    progress_percent: int,
    files_done: int = 0,
    files_total: int | None = None,
    started_at: float | None = None,
    current_file: str = "-",
    keyboard: InlineKeyboardMarkup | None = None,
    force: bool = False,
) -> None:
    services = services_from_context(context)
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    uid = int(user.id)
    elapsed = max(0.0, time.monotonic() - started_at) if started_at else 0.0

    panel = _render_status_panel(
        status=status,
        current_task=task,
        progress_percent=progress_percent,
        files_done=files_done,
        files_total=files_total,
        elapsed_seconds=elapsed,
        current_file=current_file,
    )
    panel = clamp_text(panel, limit=3900)

    session = await _load_session(services, uid)
    status_state = session.get(SESSION_STATUS_KEY, {})
    if not isinstance(status_state, dict):
        status_state = {}

    last_edit = float(status_state.get("last_edit_monotonic", 0.0))
    if not force and time.monotonic() - last_edit < STATUS_EDIT_MIN_INTERVAL_SECONDS:
        return

    msg_id = status_state.get("message_id")
    chat_id = status_state.get("chat_id")
    sent_message = None
    if msg_id and chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=int(msg_id),
                text=panel,
                reply_markup=keyboard or _status_keyboard(),
            )
            status_state["last_edit_monotonic"] = time.monotonic()
            session[SESSION_STATUS_KEY] = status_state
            await _save_session(services, uid, session)
            return
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                services.logger.warning("Status panel edit failed: %s", exc)
        except Exception:
            pass

    sent_message = await message.reply_text(panel, reply_markup=keyboard or _status_keyboard())
    status_state = {
        "chat_id": int(sent_message.chat_id),
        "message_id": int(sent_message.message_id),
        "last_edit_monotonic": time.monotonic(),
        "status": status,
        "task": task,
        "progress_percent": int(progress_percent),
        "files_done": int(files_done),
        "files_total": files_total,
        "current_file": current_file,
        "updated_at": utc_now_iso(),
    }
    session[SESSION_STATUS_KEY] = status_state
    await _save_session(services, uid, session)


async def _set_pending_generation(
    services: BotServices,
    user_id: int,
    action: str,
    prompt: str,
    enhanced_prompt: str,
    plan: str,
    expected_files: list[str] | None = None,
) -> None:
    session = await _load_session(services, user_id)
    session[SESSION_PENDING_KEY] = {
        "action": action,
        "prompt": prompt,
        "enhanced_prompt": enhanced_prompt,
        "plan": plan,
        "expected_files": expected_files or [],
        "stop_requested": False,
        "created_at": utc_now_iso(),
    }
    session["last_user_prompt"] = prompt
    session["last_enhanced_prompt"] = enhanced_prompt
    await _save_session(services, user_id, session)


async def _clear_pending_generation(services: BotServices, user_id: int) -> None:
    session = await _load_session(services, user_id)
    session.pop(SESSION_PENDING_KEY, None)
    await _save_session(services, user_id, session)


async def _get_pending_generation(services: BotServices, user_id: int) -> dict[str, Any] | None:
    session = await _load_session(services, user_id)
    value = session.get(SESSION_PENDING_KEY)
    if isinstance(value, dict):
        return value
    return None


async def _set_stop_requested(services: BotServices, user_id: int, requested: bool) -> None:
    session = await _load_session(services, user_id)
    pending = session.get(SESSION_PENDING_KEY)
    if isinstance(pending, dict):
        pending["stop_requested"] = requested
        await _save_session(services, user_id, session)


async def _is_stop_requested(services: BotServices, user_id: int) -> bool:
    pending = await _get_pending_generation(services, user_id)
    return bool(isinstance(pending, dict) and pending.get("stop_requested"))


async def _chat_list_text(services: BotServices, user_id: int) -> tuple[str, list[dict[str, Any]]]:
    chats = await services.memory.list_chats(user_id, limit=10)
    active_id = await services.memory.get_active_chat_id(user_id)
    lines = ["🧠 Recent Chats\n"]
    for idx, chat in enumerate(chats, start=1):
        title = str(chat.get("title", "Untitled Chat"))
        updated = str(chat.get("updated_at", ""))[:19].replace("T", " ")
        marker = " • ACTIVE" if str(chat.get("id", "")) == active_id else ""
        lines.append(f"{idx}. {title}{marker}\n   {updated}")
    return "\n".join(lines), chats


def _prompt_debug_enabled(session: dict[str, Any]) -> bool:
    env_enabled = is_truthy_env(os.getenv(PROMPT_ENHANCEMENT_DEBUG_ENV))
    session_enabled = bool(session.get("prompt_enhancement_debug"))
    return env_enabled or session_enabled


def _sanitize_plan_internal_details(plan_text: str, enhanced_prompt: str, debug_enabled: bool) -> str:
    if debug_enabled:
        return plan_text
    text = plan_text
    if enhanced_prompt:
        text = text.replace(enhanced_prompt, "[internal specification hidden]")
    sanitized_lines: list[str] = []
    for line in text.splitlines():
        lowered = line.strip().lower()
        if "internal enhanced specification" in lowered:
            continue
        sanitized_lines.append(line)
    return "\n".join(sanitized_lines).strip()


async def _run_planning_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    user_prompt: str,
) -> None:
    services = services_from_context(context)
    uid = int(update.effective_user.id)
    active_chat_id = await services.memory.get_active_chat_id(uid)
    session = await _load_session(services, uid)
    debug_enabled = _prompt_debug_enabled(session)
    enhancement = services.ai.enhance_prompt(user_prompt, task_type=action)
    enhanced_prompt = normalize_whitespace(enhancement.enhanced_prompt)
    started = time.monotonic()

    planning_prompt = (
        "Return ONLY a concise implementation plan for the requested task.\n"
        "Do NOT output source code and do NOT output FILE blocks.\n\n"
        "Your plan must contain ONLY these sections:\n"
        "1) Project Plan\n"
        "2) UX Direction and Visual Style\n"
        "3) Component Strategy\n"
        "4) Responsiveness Strategy\n"
        "5) Animation Strategy\n"
        "6) SVG and Icon Strategy\n"
        "7) Minimal File Tree\n"
        "8) Required Files Only\n"
        "9) Short Explanation\n\n"
        "Keep it concise and minimal.\n"
        f"Task type: {action}\n"
        f"User request: {user_prompt}"
    )
    planning_system_prompt = (
        "You are a senior full-stack engineer, senior UI/UX designer, and product architect.\n"
        "Use this internal upgraded specification to guide planning quality and production standards.\n"
        f"{enhanced_prompt}"
    )

    try:
        await _upsert_status_message(
            update,
            context,
            status="Planning Project",
            task="Analyzing requirements",
            progress_percent=12,
            files_done=0,
            files_total=None,
            started_at=started,
            current_file="-",
            keyboard=_status_keyboard(include_continue=False),
            force=True,
        )

        plan_chunks: list[str] = []
        last_progress = 12
        async for chunk in services.ai.stream(
            uid,
            planning_prompt,
            system_prompt=planning_system_prompt,
            chat_id=active_chat_id,
        ):
            plan_chunks.append(chunk)
            size = len("".join(plan_chunks))
            progress = min(90, 12 + (size // 60))
            if progress - last_progress >= 5:
                await _upsert_status_message(
                    update,
                    context,
                    status="Planning Project",
                    task="Drafting architecture",
                    progress_percent=progress,
                    files_done=0,
                    files_total=None,
                    started_at=started,
                    current_file="plan.txt",
                )
                last_progress = progress

        plan_text = "".join(plan_chunks).strip()
        plan_text = _sanitize_plan_internal_details(plan_text, enhanced_prompt=enhanced_prompt, debug_enabled=debug_enabled)
        if not plan_text:
            raise AIEngineError("Planning response was empty")

        expected_files = _extract_required_files(plan_text)
        await _set_pending_generation(
            services,
            uid,
            action=action,
            prompt=enhancement.original_prompt,
            enhanced_prompt=enhanced_prompt,
            plan=plan_text,
            expected_files=expected_files,
        )

        await services.files.write_file(uid, "plan.txt", plan_text, project_name="default")
        plan_path = services.files.user_file_path(uid, "plan.txt", project_name="default")
        async with aiofiles.open(plan_path, "rb") as plan_doc:
            plan_bytes = await plan_doc.read()
        await update.effective_message.reply_document(
            document=InputFile(io.BytesIO(plan_bytes), filename="plan.txt"),
            filename="plan.txt",
            caption="📝 Plan ready. Tap CONTINUE to generate files.",
            reply_markup=_status_keyboard(include_continue=True),
        )
        if debug_enabled:
            try:
                await update.effective_message.reply_text(clamp_text(f"🧪 Debug enhanced prompt:\n{enhanced_prompt}"))
            except Exception as debug_exc:
                services.logger.warning("Failed to send debug enhanced prompt: %s", debug_exc)
        await _upsert_status_message(
            update,
            context,
            status="Plan Ready",
            task="Awaiting approval to continue",
            progress_percent=100,
            files_done=0,
            files_total=len(expected_files) if expected_files else None,
            started_at=started,
            current_file="plan.txt",
            keyboard=_status_keyboard(include_continue=True),
            force=True,
        )
    except asyncio.CancelledError:
        await _upsert_status_message(
            update,
            context,
            status="Planning Cancelled",
            task="Operation cancelled",
            progress_percent=100,
            files_done=0,
            files_total=None,
            started_at=started,
            current_file="-",
            keyboard=_status_keyboard(include_continue=True),
            force=True,
        )
        raise
    except (AIEngineError, SecurityError, Exception) as exc:
        services.logger.exception("Planning mode failed")
        await services.memory.increment_errors()
        await _upsert_status_message(
            update,
            context,
            status="Planning Failed",
            task=str(exc),
            progress_percent=100,
            files_done=0,
            files_total=None,
            started_at=started,
            current_file="-",
            keyboard=_status_keyboard(include_continue=True),
            force=True,
        )


async def _run_generation_from_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    uid = int(update.effective_user.id)
    active_chat_id = await services.memory.get_active_chat_id(uid)
    pending = await _get_pending_generation(services, uid)
    if not pending:
        await update.effective_message.reply_text("No pending plan found. Use /build or /fix first.")
        return

    action = str(pending.get("action", "build"))
    user_prompt = str(pending.get("prompt", "")).strip()
    enhanced_prompt = normalize_whitespace(str(pending.get("enhanced_prompt", "")).strip())
    if not enhanced_prompt:
        enhanced_prompt = normalize_whitespace(services.ai.enhance_prompt(user_prompt, task_type=action).enhanced_prompt)
    plan_text = str(pending.get("plan", "")).strip()
    expected_files = pending.get("expected_files")
    if not isinstance(expected_files, list):
        expected_files = []
    total_expected = len(expected_files) if expected_files else None
    if not user_prompt:
        await update.effective_message.reply_text("Pending task is invalid. Start again with /build.")
        await _clear_pending_generation(services, uid)
        return

    await _set_stop_requested(services, uid, requested=False)
    generation_prompt = (
        "Generate the project now from the approved plan.\n"
        "Output ONLY file blocks using this exact format:\n"
        "FILE: path/to/file.ext\n```language\n<full content>\n```\n\n"
        "Requirements:\n"
        "- Generate ONLY required files\n"
        "- Do NOT add optional files\n"
        "- Include full file content, no placeholders\n"
        "- Preserve original file extensions and folder structure\n"
        "- One file block per file\n\n"
        "Apply premium engineering and product quality standards automatically.\n"
        "Use modern UX defaults, accessibility, responsive behavior, polished states, and SVG-first assets when relevant.\n"
        f"Approved plan:\n{plan_text}\n\n"
        f"Task type: {action}\n"
        f"User request: {user_prompt}"
    )
    generation_system_prompt = (
        "You are a senior full-stack engineer, senior UI/UX designer, and product architect.\n"
        "Follow this internal upgraded specification when generating files:\n"
        f"{enhanced_prompt or user_prompt}"
    )

    content = ""
    processed_blocks = 0
    saved_count = 0
    current_file = "-"
    canceled = False
    started = time.monotonic()

    await _upsert_status_message(
        update,
        context,
        status="Generating Files",
        task="Preparing workspace",
        progress_percent=8,
        files_done=0,
        files_total=total_expected,
        started_at=started,
        current_file="-",
        keyboard=_status_keyboard(include_continue=False),
        force=True,
    )

    try:
        async for chunk in services.ai.stream(
            uid,
            generation_prompt,
            system_prompt=generation_system_prompt,
            chat_id=active_chat_id,
        ):
            content += chunk
            if await _is_stop_requested(services, uid):
                canceled = True
                break

            blocks = services.ai.extract_file_blocks(content)
            if len(blocks) <= processed_blocks:
                continue

            for item in blocks[processed_blocks:]:
                if await _is_stop_requested(services, uid):
                    canceled = True
                    break
                current_file = item.path
                await _upsert_status_message(
                    update,
                    context,
                    status="Generating Files",
                    task=f"Generating {item.path}",
                    progress_percent=min(95, 12 + saved_count * 8),
                    files_done=saved_count,
                    files_total=total_expected,
                    started_at=started,
                    current_file=item.path,
                    keyboard=_status_keyboard(include_continue=False),
                )

                await services.files.write_file(uid, item.path, item.content, project_name="default")
                saved_count += 1

                file_buffer = io.BytesIO(item.content.encode("utf-8"))
                filename = Path(item.path).name
                await update.effective_message.reply_document(
                    document=InputFile(file_buffer, filename=filename),
                    filename=filename,
                    caption=f"✅ Generated: {item.path}",
                )

                if total_expected is not None and total_expected > 0:
                    pct = min(95, int((saved_count / total_expected) * 100))
                else:
                    pct = min(95, 12 + saved_count * 10)
                await _upsert_status_message(
                    update,
                    context,
                    status="Generating Files",
                    task=f"Generated {item.path}",
                    progress_percent=pct,
                    files_done=saved_count,
                    files_total=total_expected,
                    started_at=started,
                    current_file=item.path,
                    keyboard=_status_keyboard(include_continue=False),
                )

            processed_blocks = len(blocks)
            if canceled:
                break

        if canceled:
            await _upsert_status_message(
                update,
                context,
                status="Generation Stopped",
                task="Stopped safely by user",
                progress_percent=100,
                files_done=saved_count,
                files_total=total_expected,
                started_at=started,
                current_file=current_file,
                keyboard=_status_keyboard(include_continue=True),
                force=True,
            )
            return

        if saved_count == 0:
            await _upsert_status_message(
                update,
                context,
                status="Generation Failed",
                task="No valid FILE blocks returned",
                progress_percent=100,
                files_done=0,
                files_total=total_expected,
                started_at=started,
                current_file="-",
                keyboard=_status_keyboard(include_continue=True),
                force=True,
            )
            return

        await _upsert_status_message(
            update,
            context,
            status="Creating ZIP",
            task="Compressing project files",
            progress_percent=96,
            files_done=saved_count,
            files_total=total_expected or saved_count,
            started_at=started,
            current_file=current_file,
            keyboard=_status_keyboard(include_continue=False),
            force=True,
        )
        zip_path = services.files.export_zip(uid, filename="project.zip")

        await _upsert_status_message(
            update,
            context,
            status="Sending ZIP",
            task="Uploading archive",
            progress_percent=99,
            files_done=saved_count,
            files_total=total_expected or saved_count,
            started_at=started,
            current_file=Path(zip_path).name,
            keyboard=_status_keyboard(include_continue=False),
            force=True,
        )
        async with aiofiles.open(zip_path, "rb") as archive:
            zip_bytes = await archive.read()
        await update.effective_message.reply_document(
            document=InputFile(io.BytesIO(zip_bytes), filename=Path(zip_path).name),
            filename=Path(zip_path).name,
            caption="📦 Project ZIP ready",
        )

        session = await _load_session(services, uid)
        session["last_zip_name"] = Path(zip_path).name
        await _save_session(services, uid, session)
        await _clear_pending_generation(services, uid)
        await _upsert_status_message(
            update,
            context,
            status="Completed",
            task="Project generated successfully",
            progress_percent=100,
            files_done=saved_count,
            files_total=total_expected or saved_count,
            started_at=started,
            current_file=current_file,
            keyboard=_completion_keyboard(),
            force=True,
        )
    except asyncio.CancelledError:
        await _upsert_status_message(
            update,
            context,
            status="Generation Cancelled",
            task="Operation cancelled",
            progress_percent=100,
            files_done=saved_count,
            files_total=total_expected,
            started_at=started,
            current_file=current_file,
            keyboard=_status_keyboard(include_continue=True),
            force=True,
        )
        raise
    except (AIEngineError, SecurityError, Exception) as exc:
        services.logger.exception("Generation mode failed")
        await services.memory.increment_errors()
        await _upsert_status_message(
            update,
            context,
            status="Generation Failed",
            task=str(exc),
            progress_percent=100,
            files_done=saved_count,
            files_total=total_expected,
            started_at=started,
            current_file=current_file,
            keyboard=_status_keyboard(include_continue=True),
            force=True,
        )


async def _run_user_guarded(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    operation: Callable[[], Awaitable[None]],
    use_queue: bool = True,
) -> None:
    """Run a user operation behind access checks and optional per-user queueing."""
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    uid = int(update.effective_user.id)
    if not use_queue:
        await operation()
        return
    await services.middleware.enter_user_queue(uid)
    try:
        await operation()
    finally:
        await services.middleware.leave_user_queue(uid)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        services = services_from_context(context)
        uid = int(update.effective_user.id)
        await services.memory.get_active_chat_id(uid)
        await update.effective_message.reply_text(
            "✨ Welcome to your AI Workspace.\nUse /build or /fix to start, then CONTINUE to generate files."
        )
        await _upsert_status_message(
            update,
            context,
            status="Ready",
            task=DEFAULT_STATUS_TEXT,
            progress_percent=100,
            files_done=0,
            files_total=None,
            started_at=time.monotonic(),
            current_file="-",
            keyboard=_status_keyboard(include_continue=True),
            force=True,
        )

    await _run_user_guarded(update, context, _impl, use_queue=False)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        await update.effective_message.reply_text(
            "🧭 Commands:\n"
            "/build, /fix, /continue, /stop\n"
            "/newchat, /chats, /history\n"
            "/run, /files, /zip, /delete, /reset, /status"
        )

    await _run_user_guarded(update, context, _impl)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        services = services_from_context(context)
        stats = get_system_stats()
        persistent = await services.memory.get_stats()
        text = format_stats_message(
            stats,
            active_users=len(persistent.get("active_users", [])),
            api_calls=int(persistent.get("api_calls", 0)),
            error_count=int(persistent.get("errors", 0)),
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        await _upsert_status_message(
            update,
            context,
            status="Status Refreshed",
            task="System metrics checked",
            progress_percent=100,
            files_done=0,
            files_total=None,
            started_at=time.monotonic(),
            current_file="-",
            keyboard=_status_keyboard(include_continue=True),
            force=True,
        )

    await _run_user_guarded(update, context, _impl)


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        services = services_from_context(context)
        uid = int(update.effective_user.id)
        active_chat_id = await services.memory.get_active_chat_id(uid)
        active_chat = await services.memory.get_chat(uid, active_chat_id)
        recent = await services.memory.get_recent_history(uid, limit=12, chat_id=active_chat_id)
        if not recent:
            await update.effective_message.reply_text("No messages yet in this chat.")
            return
        title = active_chat.get("title", "Active Chat") if active_chat else "Active Chat"
        lines = [f"🧠 {title}\n"]
        for item in recent:
            role = str(item.get("role", "user")).upper()
            content = str(item.get("content", "")).strip().replace("\n", " ")
            lines.append(f"{role}: {content[:180]}")
        await update.effective_message.reply_text(clamp_text("\n".join(lines)))

    await _run_user_guarded(update, context, _impl)


async def newchat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        services = services_from_context(context)
        uid = int(update.effective_user.id)
        title = " ".join(context.args).strip() if context.args else "New Chat"
        chat = await services.memory.create_chat(uid, title=title, set_active=True)
        await update.effective_message.reply_text(f"🆕 Chat created and opened: {chat.get('title', 'New Chat')}")
        text, chats = await _chat_list_text(services, uid)
        await update.effective_message.reply_text(text, reply_markup=_chat_actions_keyboard(chats))

    await _run_user_guarded(update, context, _impl)


async def chats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        services = services_from_context(context)
        uid = int(update.effective_user.id)
        text, chats = await _chat_list_text(services, uid)
        await update.effective_message.reply_text(text, reply_markup=_chat_actions_keyboard(chats))

    await _run_user_guarded(update, context, _impl)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        services = services_from_context(context)
        uid = int(update.effective_user.id)
        await services.memory.reset_user(uid)
        await services.memory.create_chat(uid, title="New Chat", set_active=True)
        await update.effective_message.reply_text("✅ Your workspace and chat history were reset.")

    await _run_user_guarded(update, context, _impl)


async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        services = services_from_context(context)
        uid = int(update.effective_user.id)
        files = services.files.list_files(uid, "default")
        if not files:
            await update.effective_message.reply_text("No files in your project yet.")
            return
        await update.effective_message.reply_text(clamp_text("📁 Files:\n" + "\n".join(f"- {f}" for f in files)))

    await _run_user_guarded(update, context, _impl)


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        services = services_from_context(context)
        if not context.args:
            await update.effective_message.reply_text("Usage: /delete RELATIVE_FILE_PATH")
            return
        uid = int(update.effective_user.id)
        target = " ".join(context.args)
        ok = services.files.delete_file(uid, target)
        await update.effective_message.reply_text("🗑 Deleted." if ok else "❌ File not found.")

    await _run_user_guarded(update, context, _impl)


async def zip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        await _send_zip(update, context)

    await _run_user_guarded(update, context, _impl)


async def _send_zip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    uid = int(update.effective_user.id)
    await _upsert_status_message(
        update,
        context,
        status="Preparing ZIP",
        task="Creating latest project archive",
        progress_percent=90,
        files_done=0,
        files_total=None,
        started_at=time.monotonic(),
        current_file="project.zip",
        keyboard=_status_keyboard(include_continue=True),
        force=True,
    )
    zip_path = services.files.export_zip(uid, filename="project.zip")
    async with aiofiles.open(zip_path, "rb") as archive:
        zip_bytes = await archive.read()
    await update.effective_message.reply_document(
        document=InputFile(io.BytesIO(zip_bytes), filename=Path(zip_path).name),
        filename=Path(zip_path).name,
        caption="📦 Latest project ZIP",
    )
    await _upsert_status_message(
        update,
        context,
        status="ZIP Sent",
        task="Archive delivered",
        progress_percent=100,
        files_done=0,
        files_total=None,
        started_at=time.monotonic(),
        current_file=Path(zip_path).name,
        keyboard=_completion_keyboard(),
        force=True,
    )


async def build_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        services = services_from_context(context)
        prompt = " ".join(context.args).strip() if context.args else "Build a complete production-ready app."
        prompt = services.security.validate_user_text(prompt, max_length=services.config.config.limits.max_prompt_chars)
        await _run_planning_mode(update, context, action="build", user_prompt=prompt)

    await _run_user_guarded(update, context, _impl)


async def fix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        services = services_from_context(context)
        prompt = " ".join(context.args).strip() if context.args else "Fix errors in the current project."
        prompt = services.security.validate_user_text(prompt, max_length=services.config.config.limits.max_prompt_chars)
        await _run_planning_mode(update, context, action="fix", user_prompt=prompt)

    await _run_user_guarded(update, context, _impl)


async def continue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        await _run_generation_from_pending(update, context)

    await _run_user_guarded(update, context, _impl)


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        services = services_from_context(context)
        uid = int(update.effective_user.id)
        pending = await _get_pending_generation(services, uid)
        if not pending:
            await update.effective_message.reply_text("No active generation to stop.")
            return
        await _set_stop_requested(services, uid, requested=True)
        await _upsert_status_message(
            update,
            context,
            status="Stopping",
            task="Stop requested by user",
            progress_percent=100,
            files_done=0,
            files_total=None,
            started_at=time.monotonic(),
            current_file="-",
            keyboard=_status_keyboard(include_continue=True),
            force=True,
        )
        await update.effective_message.reply_text("⛔ Stop requested. Generation will stop safely.")

    await _run_user_guarded(update, context, _impl)


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        services = services_from_context(context)
        uid = int(update.effective_user.id)
        project_dir = str(services.files.user_project_dir(uid))
        cmd = " ".join(context.args).strip() if context.args else "python3 main.py"
        cmd = services.security.validate_command(cmd, services.docker.allowed_commands)
        msg = await update.effective_message.reply_text(
            f"🐳 Running in Docker:\n`{safe_markdown(cmd)}`", parse_mode=ParseMode.MARKDOWN_V2
        )
        result = await services.docker.run(project_dir, cmd)
        body = (
            f"Exit: {result.exit_code}\n"
            f"Timed out: {result.timed_out}\n\n"
            f"STDOUT:\n{result.stdout or '(empty)'}\n\n"
            f"STDERR:\n{result.stderr or '(empty)'}"
        )
        await msg.edit_text(clamp_text(body))

    await _run_user_guarded(update, context, _impl)


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async def _impl() -> None:
        services = services_from_context(context)
        uid = int(update.effective_user.id)
        session = await _load_session(services, uid)
        text = (update.effective_message.text or "").strip()

        pending_rename = session.get(SESSION_RENAME_CHAT_KEY)
        if isinstance(pending_rename, str) and pending_rename:
            ok = await services.memory.rename_chat(uid, pending_rename, text)
            session.pop(SESSION_RENAME_CHAT_KEY, None)
            await _save_session(services, uid, session)
            await update.effective_message.reply_text("✅ Chat renamed." if ok else "❌ Rename failed.")
            return

        if should_trigger_continue(text):
            await _run_generation_from_pending(update, context)
            return

        await update.effective_message.reply_text(
            "Use /build <prompt> to start planning, then CONTINUE to generate files.",
            reply_markup=_status_keyboard(include_continue=True),
        )

    await _run_user_guarded(update, context, _impl)


async def _handle_chat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    services = services_from_context(context)
    uid = int(update.effective_user.id)
    query = update.callback_query
    parts = data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid chat action.", show_alert=True)
        return
    action, chat_id = parts[1], parts[2]
    if action == "open":
        ok = await services.memory.set_active_chat(uid, chat_id)
        if ok:
            chat = await services.memory.get_chat(uid, chat_id)
            title = chat.get("title", "Chat") if chat else "Chat"
            await query.message.reply_text(f"📂 Opened chat: {title}")
        else:
            await query.answer("Chat not found.", show_alert=True)
    elif action == "delete":
        ok = await services.memory.delete_chat(uid, chat_id)
        await query.message.reply_text("🗑 Chat deleted." if ok else "❌ Chat not found.")
    elif action == "rename":
        session = await _load_session(services, uid)
        session[SESSION_RENAME_CHAT_KEY] = chat_id
        await _save_session(services, uid, session)
        await query.message.reply_text("✏️ Send the new chat name as your next message.")
    text, chats = await _chat_list_text(services, uid)
    await query.message.reply_text(text, reply_markup=_chat_actions_keyboard(chats))


async def _handle_admin_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    services = services_from_context(context)
    query = update.callback_query
    user = update.effective_user
    if not user or not services.config.is_admin(int(user.id)):
        await query.answer("Admin only action.", show_alert=True)
        return
    parts = data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid approval action.", show_alert=True)
        return
    action = parts[1]
    target_uid = int(parts[2])
    if action == "approve":
        await services.middleware.approve_user(target_uid)
        try:
            await context.bot.send_message(chat_id=target_uid, text="✅ Your access has been approved. Welcome!")
        except Exception:
            pass
        await query.edit_message_text(f"✅ User {target_uid} approved.")
        await query.answer("Approved")
    elif action == "deny":
        await services.middleware.deny_user(target_uid)
        try:
            await context.bot.send_message(chat_id=target_uid, text="❌ Your access request was denied.")
        except Exception:
            pass
        await query.edit_message_text(f"❌ User {target_uid} denied.")
        await query.answer("Denied")


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    try:
        await query.answer()
    except TelegramError as exc:
        services.logger.warning("Failed to answer callback query: %s", exc)

    try:
        if data.startswith("admin:"):
            await _handle_admin_approval_callback(update, context, data)
            return

        if not await services.middleware.ensure_user_allowed(update, context):
            return
        uid = int(update.effective_user.id)
        if data == "cmd:stop":
            await _set_stop_requested(services, uid, requested=True)
            await query.message.reply_text("⛔ Stop requested.")
            return
        await services.middleware.enter_user_queue(uid)
        try:
            if data == "cmd:continue":
                await _run_generation_from_pending(update, context)
            elif data == "cmd:status":
                await _upsert_status_message(
                    update,
                    context,
                    status="Status Refreshed",
                    task="Live panel updated",
                    progress_percent=100,
                    files_done=0,
                    files_total=None,
                    started_at=time.monotonic(),
                    current_file="-",
                    keyboard=_status_keyboard(include_continue=True),
                    force=True,
                )
            elif data == "cmd:zip":
                await _send_zip(update, context)
            elif data == "cmd:chats":
                text, chats = await _chat_list_text(services, uid)
                await query.message.reply_text(text, reply_markup=_chat_actions_keyboard(chats))
            elif data == "cmd:newchat":
                chat = await services.memory.create_chat(uid, title="New Chat", set_active=True)
                await query.message.reply_text(f"🆕 Opened: {chat.get('title', 'New Chat')}")
            elif data.startswith("chat:"):
                await _handle_chat_callback(update, context, data)
            else:
                await query.answer("Unknown action.", show_alert=False)
        finally:
            await services.middleware.leave_user_queue(uid)
    except Exception as exc:
        services.logger.exception("Callback handling failed")
        await services.memory.increment_errors()
        try:
            await query.message.reply_text(clamp_text(f"❌ Button action failed: {exc}"))
        except Exception:
            pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    services: BotServices = context.application.bot_data["services"]
    services.logger.exception("Unhandled exception", exc_info=context.error)


async def post_init(application: Application) -> None:
    services: BotServices = application.bot_data["services"]
    services.logger.info("Bot initialized successfully")


def main() -> None:
    root_dir = str(Path(__file__).resolve().parent)
    services = BotServices(root_dir)

    token = services.config.get_bot_token()

    app = Application.builder().token(token).post_init(post_init).build()
    app.bot_data["services"] = services

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("build", build_cmd))
    app.add_handler(CommandHandler("fix", fix_cmd))
    app.add_handler(CommandHandler("continue", continue_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("run", run_cmd))
    app.add_handler(CommandHandler("zip", zip_cmd))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("newchat", newchat_cmd))
    app.add_handler(CommandHandler("chats", chats_cmd))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(callback_router, pattern=r"^(cmd|chat|admin):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    admin_handlers = AdminHandlers(root_dir, services.config, services.memory, services.files, services.middleware)
    register_admin_handlers(app, admin_handlers)

    app.add_error_handler(error_handler)
    services.logger.info("Starting bot polling")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    if os.name != "nt":
        try:
            import uvloop  # type: ignore

            uvloop.install()
        except Exception:
            pass
    main()
