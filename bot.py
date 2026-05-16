"""Main Telegram bot entrypoint and command routing."""

from __future__ import annotations

import io
import os
import re
import time
from pathlib import Path
from typing import Any

import aiofiles
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
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
    clamp_text,
    format_stats_message,
    get_system_stats,
    safe_markdown,
    setup_logging,
    should_trigger_continue,
    utc_now_iso,
)

SESSION_PENDING_KEY = "pending_generation"
STATUS_EDIT_MIN_INTERVAL_SECONDS = 1.5


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


def action_keyboard(include_continue: bool = True, include_stop: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if include_continue:
        rows.append([InlineKeyboardButton("▶ CONTINUE", callback_data="cmd:continue")])
    if include_stop:
        rows.append([InlineKeyboardButton("⛔ STOP", callback_data="cmd:stop")])
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


async def _set_pending_generation(
    services: BotServices,
    user_id: int,
    action: str,
    prompt: str,
    plan: str,
    expected_files: list[str] | None = None,
) -> None:
    session = await _load_session(services, user_id)
    session[SESSION_PENDING_KEY] = {
        "action": action,
        "prompt": prompt,
        "plan": plan,
        "expected_files": expected_files or [],
        "stop_requested": False,
        "created_at": utc_now_iso(),
    }
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return

    await update.effective_message.reply_text(
        "👋 Welcome to AIAGENT!\nUse /build <prompt> (or /fix <prompt>), then CONTINUE or /stop.",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return

    message = (
        "User Commands:\n"
        "/start, /help, /build, /fix, /continue, /run, /zip, /files,\n"
        "/delete, /history, /reset, /status\n\n"
        "Workflow:\n"
        "1) /build <task> or /fix <task> generates only plan.txt\n"
        "2) Use CONTINUE to generate files, STOP to cancel safely\n"
        "3) Bot auto-sends each file and then auto-sends ZIP"
    )
    await update.effective_message.reply_text(message)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return

    stats = get_system_stats()
    persistent = await services.memory.get_stats()
    text = format_stats_message(
        stats,
        active_users=len(persistent.get("active_users", [])),
        api_calls=int(persistent.get("api_calls", 0)),
        error_count=int(persistent.get("errors", 0)),
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    uid = int(update.effective_user.id)
    recent = await services.memory.get_recent_history(uid, limit=12)
    if not recent:
        await update.effective_message.reply_text("No history yet.")
        return
    lines = [f"{item['role']}: {item['content'][:140]}" for item in recent]
    await update.effective_message.reply_text(clamp_text("\n".join(lines)))


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    uid = int(update.effective_user.id)
    await services.memory.reset_user(uid)
    await update.effective_message.reply_text("✅ Your session and history were reset.")


async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    uid = int(update.effective_user.id)
    project_name = "default"
    files = services.files.list_files(uid, project_name)
    if not files:
        await update.effective_message.reply_text("No files in your project yet.")
        return
    await update.effective_message.reply_text(clamp_text("📁 Files:\n" + "\n".join(f"- {f}" for f in files)))


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /delete RELATIVE_FILE_PATH")
        return
    uid = int(update.effective_user.id)
    target = " ".join(context.args)
    ok = services.files.delete_file(uid, target)
    await update.effective_message.reply_text("🗑 Deleted." if ok else "❌ File not found.")


async def zip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    uid = int(update.effective_user.id)
    zip_path = services.files.export_zip(uid)
    async with aiofiles.open(zip_path, "rb") as archive:
        zip_bytes = await archive.read()
    await update.effective_message.reply_document(
        document=InputFile(io.BytesIO(zip_bytes), filename=Path(zip_path).name),
        filename=Path(zip_path).name,
        caption="📦 Project ZIP",
    )


async def _run_planning_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    user_prompt: str,
) -> None:
    services = services_from_context(context)
    uid = int(update.effective_user.id)

    planning_prompt = (
        "Return ONLY a simple project plan for the requested task. "
        "Do NOT output any code and do NOT output FILE blocks.\n\n"
        "Your plan must contain ONLY these sections:\n"
        "1) Project Plan\n"
        "2) Minimal File Tree\n"
        "3) Required Files Only\n"
        "4) Short Explanation\n\n"
        "Keep it concise and minimal. Avoid overengineering.\n"
        f"Task type: {action}\n"
        f"User request: {user_prompt}"
    )

    try:
        plan_response = await services.ai.ask(uid, planning_prompt)
        plan_text = plan_response.text.strip()
        if not plan_text:
            raise AIEngineError("Planning response was empty")

        expected_files = _extract_required_files(plan_text)
        await _set_pending_generation(
            services,
            uid,
            action=action,
            prompt=user_prompt,
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
            reply_markup=action_keyboard(include_continue=True, include_stop=True),
        )
    except (AIEngineError, SecurityError, Exception) as exc:
        services.logger.exception("Planning mode failed")
        await services.memory.increment_errors()
        await update.effective_message.reply_text(clamp_text(f"❌ Planning error: {exc}"))


async def _run_generation_from_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    uid = int(update.effective_user.id)
    pending = await _get_pending_generation(services, uid)
    if not pending:
        await update.effective_message.reply_text("No pending plan found. Use /build or /fix first.")
        return

    action = str(pending.get("action", "build"))
    user_prompt = str(pending.get("prompt", "")).strip()
    plan_text = str(pending.get("plan", "")).strip()
    expected_files = pending.get("expected_files")
    if not isinstance(expected_files, list):
        expected_files = []
    total_expected = len(expected_files)
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
        "- Do NOT add extra configs, folders, infra, or optional files\n"
        "- Include full file content, no placeholders\n"
        "- Preserve original file extensions\n"
        "- One file block per file\n\n"
        f"Approved plan:\n{plan_text}\n\n"
        f"Task type: {action}\n"
        f"User request: {user_prompt}"
    )

    progress = await update.effective_message.reply_text(
        "🤖 Generating Project\n\nCurrent File:\n-\n\nCompleted:\n0/"
        + (str(total_expected) if total_expected else "?")
    )
    content = ""
    processed_blocks = 0
    saved_count = 0
    current_file = "-"
    canceled = False
    last_status_edit_at = 0.0

    try:
        async for chunk in services.ai.stream(uid, generation_prompt):
            content += chunk

            if await _is_stop_requested(services, uid):
                canceled = True
                break

            blocks = services.ai.extract_file_blocks(content)
            if len(blocks) <= processed_blocks:
                continue

            new_blocks = blocks[processed_blocks:]
            for item in new_blocks:
                if await _is_stop_requested(services, uid):
                    canceled = True
                    break

                current_file = item.path
                await services.files.write_file(uid, item.path, item.content, project_name="default")
                saved_count += 1

                file_buffer = io.BytesIO(item.content.encode("utf-8"))
                filename = Path(item.path).name
                await update.effective_message.reply_document(
                    document=InputFile(file_buffer, filename=filename),
                    filename=filename,
                )

                now = time.monotonic()
                if now - last_status_edit_at >= STATUS_EDIT_MIN_INTERVAL_SECONDS:
                    await progress.edit_text(
                        clamp_text(
                            "🤖 Generating Project\n\n"
                            f"Current File:\n{current_file}\n\n"
                            f"Completed:\n{saved_count}/{total_expected if total_expected else '?'}"
                        )
                    )
                    last_status_edit_at = now

            processed_blocks = len(blocks)
            if canceled:
                break

        if canceled:
            await progress.edit_text(
                clamp_text(
                    "⛔ Generation stopped safely.\n\n"
                    f"Current File:\n{current_file}\n\n"
                    f"Completed:\n{saved_count}/{total_expected if total_expected else '?'}"
                ),
                reply_markup=action_keyboard(include_continue=True, include_stop=True),
            )
            return

        if saved_count == 0:
            await progress.edit_text("❌ No valid FILE blocks found in AI response. Generation aborted.")
            return

        zip_path = services.files.export_zip(uid)
        await progress.edit_text(
            clamp_text(
                "✅ Generation Completed\n\n"
                f"Current File:\n{current_file}\n\n"
                f"Completed:\n{saved_count}/{total_expected if total_expected else saved_count}"
            ),
            reply_markup=action_keyboard(include_continue=True, include_stop=True),
        )

        async with aiofiles.open(zip_path, "rb") as archive:
            zip_bytes = await archive.read()
        await update.effective_message.reply_document(
            document=InputFile(io.BytesIO(zip_bytes), filename=Path(zip_path).name),
            filename=Path(zip_path).name,
        )

        await _clear_pending_generation(services, uid)

    except (AIEngineError, SecurityError, Exception) as exc:
        services.logger.exception("Generation mode failed")
        await services.memory.increment_errors()
        await progress.edit_text(clamp_text(f"❌ Generation error: {exc}"))


async def build_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return

    prompt = " ".join(context.args).strip() if context.args else "Build a complete production-ready app."
    prompt = services.security.validate_user_text(prompt, max_length=services.config.config.limits.max_prompt_chars)
    await _run_planning_mode(update, context, action="build", user_prompt=prompt)


async def fix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return

    prompt = " ".join(context.args).strip() if context.args else "Fix errors in the current project."
    prompt = services.security.validate_user_text(prompt, max_length=services.config.config.limits.max_prompt_chars)
    await _run_planning_mode(update, context, action="fix", user_prompt=prompt)


async def continue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    await _run_generation_from_pending(update, context)


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    uid = int(update.effective_user.id)
    pending = await _get_pending_generation(services, uid)
    if not pending:
        await update.effective_message.reply_text("No active generation to stop.")
        return
    await _set_stop_requested(services, uid, requested=True)
    await update.effective_message.reply_text("⛔ Stop requested. Generation will stop safely.")


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return

    uid = int(update.effective_user.id)
    project_dir = str(services.files.user_project_dir(uid))
    cmd = " ".join(context.args).strip() if context.args else "python3 main.py"

    msg = await update.effective_message.reply_text(f"🐳 Running in Docker:\n`{safe_markdown(cmd)}`", parse_mode=ParseMode.MARKDOWN_V2)
    result = await services.docker.run(project_dir, cmd)
    body = (
        f"Exit: {result.exit_code}\n"
        f"Timed out: {result.timed_out}\n\n"
        f"STDOUT:\n{result.stdout or '(empty)'}\n\n"
        f"STDERR:\n{result.stderr or '(empty)'}"
    )
    await msg.edit_text(clamp_text(body))


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return

    text = (update.effective_message.text or "").strip()
    if should_trigger_continue(text):
        await _run_generation_from_pending(update, context)
        return

    await update.effective_message.reply_text(
        "Use /build <prompt> to start planning, then send CONTINUE to generate files.",
        reply_markup=action_keyboard(include_continue=True, include_stop=True),
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return

    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "cmd:build":
        context.args = ["Build a useful starter project"]
        await build_cmd(update, context)
    elif data == "cmd:fix":
        context.args = ["Fix project issues"]
        await fix_cmd(update, context)
    elif data == "cmd:continue":
        await continue_cmd(update, context)
    elif data == "cmd:stop":
        await stop_cmd(update, context)
    elif data == "cmd:files":
        await files_cmd(update, context)
    elif data == "cmd:zip":
        await zip_cmd(update, context)


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
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(callback_router, pattern=r"^cmd:"))
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
