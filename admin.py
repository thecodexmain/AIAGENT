"""Admin command handlers and registration."""

from __future__ import annotations

import os
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config_manager import ConfigManager
from file_manager import FileManager
from memory import MemoryManager
from middleware import MiddlewareService
from utils import clamp_text


class AdminHandlers:
    """Implements privileged administrative Telegram commands."""

    def __init__(
        self,
        root_dir: str,
        config: ConfigManager,
        memory: MemoryManager,
        files: FileManager,
        middleware: MiddlewareService,
    ) -> None:
        self.root_dir = Path(root_dir)
        self.config = config
        self.memory = memory
        self.files = files
        self.middleware = middleware

    async def _guard(self, update: Update) -> bool:
        return await self.middleware.ensure_admin(update)

    async def admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.message.reply_text(
            "🛡 Admin Commands:\n"
            "/setkey NEW_KEY\n/getkey\n/fullkey\n/setmodel MODEL\n/getmodel\n"
            "/setbaseurl URL\n/status\n/users\n/broadcast MSG\n/logs\n/restart\n"
            "/shutdown\n/clearcache\n/projects\n/deleteproject NAME\n/ban USER_ID\n/unban USER_ID\n"
            "/maintenance on|off"
        )

    async def setkey(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /setkey NEW_KEY")
            return
        key = " ".join(context.args).strip()
        self.config.set_api_key(key)
        await update.message.reply_text("✅ API key encrypted and saved.")

    async def getkey(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        key = self.config.get_api_key()
        masked = key[:4] + "..." + key[-4:] if len(key) >= 8 else "****"
        await update.message.reply_text(f"🔑 API key: {masked}")

    async def fullkey(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.message.reply_text(f"🔑 Full API key:\n{self.config.get_api_key()}")

    async def setmodel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /setmodel MODEL")
            return
        model = " ".join(context.args)
        self.config.set_model(model)
        await update.message.reply_text(f"✅ Model set to: {model}")

    async def getmodel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.message.reply_text(f"🤖 Model: {self.config.get_model()}")

    async def setbaseurl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /setbaseurl URL")
            return
        url = " ".join(context.args)
        self.config.set_base_url(url)
        await update.message.reply_text(f"✅ Base URL set to: {url}")

    async def users(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        stats = await self.memory.get_stats()
        users = stats.get("active_users", [])
        await update.message.reply_text(f"👥 Active users: {len(users)}\nIDs: {users}")

    async def broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /broadcast MESSAGE")
            return
        text = " ".join(context.args)
        stats = await self.memory.get_stats()
        users = stats.get("active_users", [])

        delivered = 0
        for user_id in users:
            try:
                await context.bot.send_message(chat_id=user_id, text=f"�� Admin Broadcast:\n{text}")
                delivered += 1
            except Exception:
                continue

        await update.message.reply_text(f"✅ Broadcast delivered to {delivered}/{len(users)} users")

    async def logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        log_path = self.root_dir / "logs" / "aiagent.log"
        if not log_path.exists():
            await update.message.reply_text("No logs found.")
            return
        content = log_path.read_text(encoding="utf-8", errors="replace")
        await update.message.reply_text(clamp_text(f"📜 Last logs:\n\n{content[-3500:]}"))

    async def clearcache(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        tmp = self.root_dir / "tmp"
        for entry in tmp.glob("*"):
            try:
                if entry.is_file():
                    entry.unlink()
            except Exception:
                continue
        await update.message.reply_text("🧹 Cache cleared.")

    async def projects(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        summary = []
        base = self.root_dir / "projects"
        for user_dir in base.glob("*"):
            if user_dir.is_dir():
                projects = [p.name for p in user_dir.glob("*") if p.is_dir()]
                summary.append(f"{user_dir.name}: {projects}")
        await update.message.reply_text(clamp_text("\n".join(summary) if summary else "No projects found."))

    async def deleteproject(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /deleteproject USER_ID PROJECT_NAME")
            return
        user_id = int(context.args[0])
        project_name = " ".join(context.args[1:])
        ok = self.files.delete_project(user_id, project_name)
        await update.message.reply_text("✅ Deleted." if ok else "❌ Project not found.")

    async def ban(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /ban USER_ID")
            return
        uid = int(context.args[0])
        self.middleware.ban(uid)
        await update.message.reply_text(f"🚫 User {uid} banned.")

    async def unban(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /unban USER_ID")
            return
        uid = int(context.args[0])
        self.middleware.unban(uid)
        await update.message.reply_text(f"✅ User {uid} unbanned.")

    async def maintenance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not context.args or context.args[0] not in {"on", "off"}:
            await update.message.reply_text("Usage: /maintenance on|off")
            return
        enabled = context.args[0] == "on"
        self.middleware.set_maintenance(enabled)
        await update.message.reply_text(f"🛠 Maintenance mode {'enabled' if enabled else 'disabled'}.")

    async def restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.message.reply_text("♻ Restarting process...")
        os._exit(0)

    async def shutdown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.message.reply_text("🛑 Shutting down process...")
        os._exit(0)



def register_admin_handlers(application: Application, handlers: AdminHandlers) -> None:
    """Registers all admin routes on a PTB application."""
    application.add_handler(CommandHandler("admin", handlers.admin))
    application.add_handler(CommandHandler("setkey", handlers.setkey))
    application.add_handler(CommandHandler("getkey", handlers.getkey))
    application.add_handler(CommandHandler("fullkey", handlers.fullkey))
    application.add_handler(CommandHandler("setmodel", handlers.setmodel))
    application.add_handler(CommandHandler("getmodel", handlers.getmodel))
    application.add_handler(CommandHandler("setbaseurl", handlers.setbaseurl))
    application.add_handler(CommandHandler("users", handlers.users))
    application.add_handler(CommandHandler("broadcast", handlers.broadcast))
    application.add_handler(CommandHandler("logs", handlers.logs))
    application.add_handler(CommandHandler("clearcache", handlers.clearcache))
    application.add_handler(CommandHandler("projects", handlers.projects))
    application.add_handler(CommandHandler("deleteproject", handlers.deleteproject))
    application.add_handler(CommandHandler("ban", handlers.ban))
    application.add_handler(CommandHandler("unban", handlers.unban))
    application.add_handler(CommandHandler("maintenance", handlers.maintenance))
    application.add_handler(CommandHandler("restart", handlers.restart))
    application.add_handler(CommandHandler("shutdown", handlers.shutdown))
