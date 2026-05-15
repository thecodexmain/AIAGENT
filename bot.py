"""Main Telegram bot entrypoint and command routing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
    build_progress_bar,
    clamp_text,
    format_stats_message,
    get_system_stats,
    safe_markdown,
    setup_logging,
    should_trigger_continue,
    summarize_saved_files,
    utc_now_iso,
)

SESSION_PENDING_KEY = "pending_generation"


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



def action_keyboard(include_continue: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if include_continue:
        rows.append([InlineKeyboardButton("▶ CONTINUE", callback_data="cmd:continue")])
    rows.append(
        [
            InlineKeyboardButton("📦 DOWNLOAD ZIP", callback_data="cmd:zip"),
            InlineKeyboardButton("📁 SHOW FILES", callback_data="cmd:files"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _load_session(services: BotServices, user_id: int) -> dict[str, Any]:
    return await services.memory.load_session(user_id)


async def _save_session(services: BotServices, user_id: int, session: dict[str, Any]) -> None:
    await services.memory.save_session(user_id, session)


async def _set_pending_generation(services: BotServices, user_id: int, action: str, prompt: str, plan: str) -> None:
    session = await _load_session(services, user_id)
    session[SESSION_PENDING_KEY] = {
        "action": action,
        "prompt": prompt,
        "plan": plan,
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    if not await services.middleware.ensure_user_allowed(update, context):
        return

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛠 Build", callback_data="cmd:build")],
            [InlineKeyboardButton("🐞 Fix", callback_data="cmd:fix"), InlineKeyboardButton("▶ CONTINUE", callback_data="cmd:continue")],
            [InlineKeyboardButton("📁 SHOW FILES", callback_data="cmd:files"), InlineKeyboardButton("📦 DOWNLOAD ZIP", callback_data="cmd:zip")],
        ]
    )
    await update.effective_message.reply_text(
        "👋 Welcome to AIAGENT!\nUse /build <prompt> to start planning, then send CONTINUE.",
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
        "Workflow:\n"
        "1) /build <task> or /fix <task> generates a project plan only\n"
        "2) Send CONTINUE or /continue to generate and save files\n"
        "3) Bot auto-exports ZIP and can resend with /zip"
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
    with open(zip_path, "rb") as archive:
        await update.effective_message.reply_document(
            document=archive,
            filename=Path(zip_path).name,
            caption="📦 Project ZIP",
            reply_markup=action_keyboard(include_continue=False),
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
        "Create a project architecture plan only. Do NOT generate any code and do NOT output any FILE blocks. "
        "The response must include:\n"
        "1) Architecture overview\n"
        "2) Folder structure tree\n"
        "3) Full file paths to be created\n"
        "4) Purpose of every file\n"
        "5) Execution flow step-by-step\n"
        "6) Dependencies and why they are needed\n"
        "End with: WAITING FOR CONTINUE\n\n"
        f"Task type: {action}\n"
        f"User request: {user_prompt}"
    )

    progress = await update.effective_message.reply_text("🧠 Planning project... " + build_progress_bar(10))
    plan_text = ""
    last_edit = 0

    try:
        async for chunk in services.ai.stream(uid, planning_prompt):
            plan_text += chunk
            if len(plan_text) - last_edit >= 350:
                last_edit = len(plan_text)
                await progress.edit_text(clamp_text("🧠 Planning...\n\n" + plan_text[-2000:]))

        await _set_pending_generation(services, uid, action=action, prompt=user_prompt, plan=plan_text)
        await progress.edit_text(
            clamp_text(
                "✅ Plan created.\n\n"
                f"{plan_text}\n\n"
                "Send CONTINUE or press the button when you want file generation to start."
            ),
            reply_markup=action_keyboard(include_continue=True),
        )
    except (AIEngineError, SecurityError, Exception) as exc:
        services.logger.exception("Planning mode failed")
        await services.memory.increment_errors()
        await progress.edit_text(clamp_text(f"❌ Planning error: {exc}"))


async def _run_generation_from_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_from_context(context)
    uid = int(update.effective_user.id)
    pending = await _get_pending_generation(services, uid)
    if not pending:
        await update.effective_message.reply_text("No pending plan found. Use /build or /fix first.")
        return

    action = str(pending.get("action", "build"))
    user_prompt = str(pending.get("prompt", "")).strip()
    if not user_prompt:
        await update.effective_message.reply_text("Pending task is invalid. Start again with /build.")
        await _clear_pending_generation(services, uid)
        return

    generation_prompt = (
        "Generate the project now from the approved plan. "
        "Output files only using this exact format and nothing else for code:\n"
        "FILE: path/to/file.ext\n```language\n<full content>\n```\n\n"
        "Requirements:\n"
        "- Include nested directories in file paths\n"
        "- Complete file contents, no placeholders\n"
        "- Production-ready Python 3.11 compatible where relevant\n"
        "- If a file is unchanged, do not include it\n\n"
        f"Task type: {action}\n"
        f"User request: {user_prompt}"
    )

    progress = await update.effective_message.reply_text("⚙️ Generating files... " + build_progress_bar(20))
    content = ""
    last_edit = 0

    try:
        async for chunk in services.ai.stream(uid, generation_prompt):
            content += chunk
            if len(content) - last_edit >= 500:
                last_edit = len(content)
                await progress.edit_text(clamp_text("⚙️ Generating files...\n\nStreaming response received."))

        blocks = services.ai.extract_file_blocks(content)
        if not blocks:
            await progress.edit_text("❌ No valid FILE blocks found in AI response. Generation aborted.")
            return

        await progress.edit_text(f"💾 Saving {len(blocks)} file(s)... " + build_progress_bar(65))
        save_payload = [(item.path, item.content, item.language) for item in blocks]
        saved = await services.files.write_files(uid, save_payload, project_name="default")

        await progress.edit_text("📦 Creating ZIP... " + build_progress_bar(90))
        zip_path = services.files.export_zip(uid)

        saved_names = [item.path for item in saved]
        summary = (
            "✅ Generation completed.\n"
            f"Saved files: {len(saved)}\n\n"
            f"{summarize_saved_files(saved_names)}"
        )
        await progress.edit_text(clamp_text(summary), reply_markup=action_keyboard(include_continue=True))

        with open(zip_path, "rb") as archive:
            await update.effective_message.reply_document(
                document=archive,
                filename=Path(zip_path).name,
                caption="📦 Auto-generated ZIP is ready.",
                reply_markup=action_keyboard(include_continue=False),
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
        reply_markup=action_keyboard(include_continue=True),
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
