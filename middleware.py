"""Middleware-like access checks for auth, bans, maintenance, and cooldown."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from aiolimiter import AsyncLimiter
from telegram import Update
from telegram.ext import ContextTypes

from config_manager import ConfigManager
from memory import MemoryManager


class MiddlewareService:
    """Provides reusable guard checks for command handlers."""

    def __init__(self, root_dir: str, config: ConfigManager, memory: MemoryManager) -> None:
        self.root_dir = Path(root_dir)
        self.config = config
        self.memory = memory
        self.bans_file = self.root_dir / "data" / "bans.json"
        self.maintenance_file = self.root_dir / "data" / "maintenance.json"

        if not self.bans_file.exists():
            self.bans_file.write_text("[]", encoding="utf-8")
        if not self.maintenance_file.exists():
            self.maintenance_file.write_text(json.dumps({"enabled": False}, indent=2), encoding="utf-8")

        rpm = max(1, self.config.config.limits.requests_per_minute)
        self.limiter = AsyncLimiter(rpm, time_period=60)
        self.last_seen = defaultdict(float)

    def _is_maintenance(self) -> bool:
        data = json.loads(self.maintenance_file.read_text(encoding="utf-8"))
        return bool(data.get("enabled", False)) or bool(self.config.config.maintenance_mode)

    def set_maintenance(self, enabled: bool) -> None:
        self.maintenance_file.write_text(json.dumps({"enabled": enabled}, indent=2), encoding="utf-8")

    def _banned_users(self) -> set[int]:
        raw = json.loads(self.bans_file.read_text(encoding="utf-8"))
        return set(int(x) for x in raw)

    def ban(self, user_id: int) -> None:
        users = self._banned_users()
        users.add(user_id)
        self.bans_file.write_text(json.dumps(sorted(users), indent=2), encoding="utf-8")

    def unban(self, user_id: int) -> None:
        users = self._banned_users()
        users.discard(user_id)
        self.bans_file.write_text(json.dumps(sorted(users), indent=2), encoding="utf-8")

    async def ensure_user_allowed(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        user = update.effective_user
        if not user:
            return False

        uid = int(user.id)
        if uid in self._banned_users():
            await update.effective_message.reply_text("🚫 You are banned from using this bot.")
            return False

        if self._is_maintenance() and not self.config.is_admin(uid):
            await update.effective_message.reply_text("🛠 Bot is currently in maintenance mode.")
            return False

        now = time.time()
        cooldown = self.config.config.limits.cooldown_seconds
        elapsed = now - self.last_seen[uid]
        if elapsed < cooldown:
            wait_for = round(cooldown - elapsed, 2)
            await update.effective_message.reply_text(f"⏱ Please wait {wait_for}s before sending another command.")
            return False

        self.last_seen[uid] = now
        await self.memory.mark_user_active(uid)

        async with self.limiter:
            return True

    async def ensure_admin(self, update: Update) -> bool:
        user = update.effective_user
        if not user:
            return False
        if not self.config.is_admin(int(user.id)):
            await update.effective_message.reply_text("🔒 Admin command only.")
            return False
        return True
