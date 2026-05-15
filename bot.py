"""Main Telegram bot entrypoint and command routing."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from admin import AdminHandlers, register_admin_handlers
from ai_engine import AIEngine, AIEngineError
from config_manager import ConfigManager
from docker_runner import DockerRunner
from file_manager import FileManager
from memory import MemoryManager
from middleware import MiddlewareService
from security import SecurityError, SecurityManager
from utils import (
    build_progress_bar,
    clamp_text,
    format_stats_message,
    get_system_stats,
    safe_markdown,
    setup_logging,
)


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
        self.files = FileManager(root_dir, self.security)
        self.middleware = MiddlewareService(root_dir, self.config, self.memory)
        self.ai = AIEngine(self.config, self.memory)
        self.docker = DockerRunner(self.config, self.security)


def services_from_context(context: ContextTypes.DEFAULT_TYPE) -> BotServices:
    return context.application.bot_data["services"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛠 Build", callback_data="cmd:build")],
            [InlineKeyboardButton("🐞 Fix", callback_data="cmd:fix"), InlineKeyboardButton("▶ Continue", callback_data="cmd:continue")],
            [InlineKeyboardButton("📁 Files", callback_data="cmd:files"), InlineKeyboardButton("📦 Zip", callback_data="cmd:zip")],
        ]
    )
    await update.message.reply_text(
        "👋 Welcome to AIAGENT!\nUse /help to see all commands.",
        reply_markup=keyboard,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return

    message = (
        "User Commands:\n"
        "/start, /help, /build, /fix, /continue, /run, /zip, /files,\n"
        "/delete, /history, /reset, /status\n\n"
        "Use /build <prompt> to generate code."
    )
    await update.message.reply_text(message)


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
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    uid = int(update.effective_user.id)
    recent = await services.memory.get_recent_history(uid, limit=12)
    if not recent:
        await update.message.reply_text("No history yet.")
        return
    lines = [f"{item['role']}: {item['content'][:140]}" for item in recent]
    await update.message.reply_text(clamp_text("\n".join(lines)))


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    uid = int(update.effective_user.id)
    await services.memory.reset_user(uid)
    await update.message.reply_text("✅ Your session and history were reset.")


async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    uid = int(update.effective_user.id)
    project_name = "default"
    files = services.files.list_files(uid, project_name)
    if not files:
        await update.message.reply_text("No files in your project yet.")
        return
    await update.message.reply_text(clamp_text("📁 Files:\n" + "\n".join(f"- {f}" for f in files)))


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /delete RELATIVE_FILE_PATH")
        return
    uid = int(update.effective_user.id)
    target = " ".join(context.args)
    ok = services.files.delete_file(uid, target)
    await update.message.reply_text("🗑 Deleted." if ok else "❌ File not found.")


async def zip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    uid = int(update.effective_user.id)
    zip_path = services.files.export_zip(uid)
    await update.message.reply_document(document=open(zip_path, "rb"), filename=f"project_{uid}.zip")


async def _run_ai_task(update: Update, context: ContextTypes.DEFAULT_TYPE, task_prompt: str) -> None:
    services = services_from_context(context)
    uid = int(update.effective_user.id)

    progress = await update.message.reply_text("🚀 Starting... " + build_progress_bar(5))
    content = ""
    last_edit = 0

    try:
        async for chunk in services.ai.stream(uid, task_prompt):
            content += chunk
            if len(content) - last_edit > 350:
                last_edit = len(content)
                await progress.edit_text(clamp_text("⏳ Working...\n\n" + content[-1200:]))

        files = services.ai.extract_files(content)
        saved_files = []
        for rel, file_content in files.items():
            await services.files.write_file(uid, rel, file_content)
            saved_files.append(rel)

        summary = "✅ Completed\n"
        if saved_files:
            summary += "\n📄 Saved files:\n" + "\n".join(f"- {f}" for f in saved_files)
        await progress.edit_text(clamp_text(summary + "\n\n" + content[:2500]))

    except (AIEngineError, SecurityError, Exception) as exc:
        services.logger.exception("AI task failed")
        await services.memory.increment_errors()
        await progress.edit_text(clamp_text(f"❌ Error: {exc}"))


async def build_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    prompt = " ".join(context.args).strip() if context.args else "Build a complete production-ready app."
    task = (
        "You are building a software project. Return normal explanation and, for each file, use this format:\n"
        "FILE: path/to/file.ext\n```language\n<full content>\n```\n\n"
        f"User request: {prompt}"
    )
    await _run_ai_task(update, context, task)


async def fix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    prompt = " ".join(context.args).strip() if context.args else "Fix errors in the current project."
    await _run_ai_task(update, context, f"Debug and fix the project. Request: {prompt}")


async def continue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return
    prompt = " ".join(context.args).strip() if context.args else "Continue previous project from memory."
    await _run_ai_task(update, context, prompt)


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return

    uid = int(update.effective_user.id)
    project_dir = str(services.files.user_project_dir(uid))
    cmd = " ".join(context.args).strip() if context.args else "python3 main.py"

    msg = await update.message.reply_text(f"🐳 Running in Docker:\n`{safe_markdown(cmd)}`", parse_mode=ParseMode.MARKDOWN_V2)
    result = await services.docker.run(project_dir, cmd)
    body = (
        f"Exit: {result.exit_code}\n"
        f"Timed out: {result.timed_out}\n\n"
        f"STDOUT:\n{result.stdout or '(empty)'}\n\n"
        f"STDERR:\n{result.stderr or '(empty)'}"
    )
    await msg.edit_text(clamp_text(body))


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    fake_update = Update(
        update_id=update.update_id,
        message=query.message,
        callback_query=query,
        effective_user=update.effective_user,
        effective_chat=update.effective_chat,
    )

    if data == "cmd:build":
        context.args = ["Build a useful starter project"]
        await build_cmd(fake_update, context)
    elif data == "cmd:fix":
        context.args = ["Fix project issues"]
        await fix_cmd(fake_update, context)
    elif data == "cmd:continue":
        context.args = ["Continue last project"]
        await continue_cmd(fake_update, context)
    elif data == "cmd:files":
        await files_cmd(fake_update, context)
    elif data == "cmd:zip":
        await zip_cmd(fake_update, context)


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
    app.add_handler(CommandHandler("run", run_cmd))
    app.add_handler(CommandHandler("zip", zip_cmd))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(callback_router, pattern=r"^cmd:"))

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
