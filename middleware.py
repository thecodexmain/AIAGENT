"""Middleware-like access checks for auth, bans, maintenance, approvals, and queueing."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from aiolimiter import AsyncLimiter
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config_manager import ConfigManager
from memory import MemoryManager
from utils import utc_now_iso


class MiddlewareService:
    """Provides reusable guard checks and per-user request queueing."""

    def __init__(self, root_dir: str, config: ConfigManager, memory: MemoryManager) -> None:
        self.root_dir = Path(root_dir)
        self.config = config
        self.memory = memory
        self.bans_file = self.root_dir / "data" / "bans.json"
        self.maintenance_file = self.root_dir / "data" / "maintenance.json"
        self.approved_users_file = self.root_dir / "data" / "approved_users.json"

        if not self.bans_file.exists():
            self.bans_file.write_text("[]", encoding="utf-8")
        if not self.maintenance_file.exists():
            self.maintenance_file.write_text(json.dumps({"enabled": False}, indent=2), encoding="utf-8")
        if not self.approved_users_file.exists():
            self.approved_users_file.write_text(
                json.dumps({"approved_users": [], "denied_users": [], "requests": {}}, indent=2),
                encoding="utf-8",
            )

        rpm = max(1, self.config.config.limits.requests_per_minute)
        self.limiter = AsyncLimiter(rpm, time_period=60)
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._file_lock = asyncio.Lock()

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

    def _load_approval_data(self) -> dict[str, Any]:
        raw = json.loads(self.approved_users_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("approved_users", [])
        raw.setdefault("denied_users", [])
        raw.setdefault("requests", {})
        return raw

    def _save_approval_data(self, payload: dict[str, Any]) -> None:
        self.approved_users_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    async def is_approved(self, user_id: int) -> bool:
        async with self._file_lock:
            data = self._load_approval_data()
            return int(user_id) in {int(uid) for uid in data.get("approved_users", [])}

    async def is_denied(self, user_id: int) -> bool:
        async with self._file_lock:
            data = self._load_approval_data()
            return int(user_id) in {int(uid) for uid in data.get("denied_users", [])}

    async def approve_user(self, user_id: int) -> None:
        async with self._file_lock:
            data = self._load_approval_data()
            approved = {int(uid) for uid in data.get("approved_users", [])}
            denied = {int(uid) for uid in data.get("denied_users", [])}
            approved.add(int(user_id))
            denied.discard(int(user_id))
            requests = data.get("requests", {})
            requests.pop(str(user_id), None)
            data["approved_users"] = sorted(approved)
            data["denied_users"] = sorted(denied)
            data["requests"] = requests
            self._save_approval_data(data)

    async def deny_user(self, user_id: int) -> None:
        async with self._file_lock:
            data = self._load_approval_data()
            approved = {int(uid) for uid in data.get("approved_users", [])}
            denied = {int(uid) for uid in data.get("denied_users", [])}
            denied.add(int(user_id))
            approved.discard(int(user_id))
            requests = data.get("requests", {})
            requests.pop(str(user_id), None)
            data["approved_users"] = sorted(approved)
            data["denied_users"] = sorted(denied)
            data["requests"] = requests
            self._save_approval_data(data)

    async def request_approval(self, update: Update, context: ContextTypes.DEFAULT_TYPE, silent_user_notice: bool = False) -> None:
        user = update.effective_user
        if not user:
            return

        uid = int(user.id)
        async with self._file_lock:
            data = self._load_approval_data()
            requests = data.get("requests", {})
            already_requested = str(uid) in requests
            if not already_requested:
                requests[str(uid)] = utc_now_iso()
                data["requests"] = requests
                self._save_approval_data(data)

        if not already_requested:
            username = f"@{user.username}" if user.username else "(none)"
            full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip() or "(no name)"
            join_time = utc_now_iso()
            text = (
                "🛡 New User Approval Request\n\n"
                f"Username: {username}\n"
                f"User ID: {uid}\n"
                f"Name: {full_name}\n"
                f"Join Time: {join_time}"
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ APPROVE", callback_data=f"admin:approve:{uid}"),
                        InlineKeyboardButton("❌ DENY", callback_data=f"admin:deny:{uid}"),
                    ]
                ]
            )
            for admin_id in self.config.config.admin_ids:
                try:
                    await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard)
                except Exception:
                    continue

        if not silent_user_notice and update.effective_message:
            await update.effective_message.reply_text(
                "⏳ Your access request is pending admin approval.\nYou'll be notified once approved."
            )

    async def ensure_user_allowed(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        user = update.effective_user
        if not user:
            return False

        uid = int(user.id)
        if uid in self._banned_users():
            if update.effective_message:
                await update.effective_message.reply_text("🚫 You are banned from using this bot.")
            return False

        if self._is_maintenance() and not self.config.is_admin(uid):
            if update.effective_message:
                await update.effective_message.reply_text("🛠 Bot is currently in maintenance mode.")
            return False

        if not self.config.is_admin(uid):
            if await self.is_denied(uid):
                if update.effective_message:
                    await update.effective_message.reply_text("❌ Your access request was denied by admins.")
                return False
            if not await self.is_approved(uid):
                await self.request_approval(update, context)
                return False

        await self.memory.mark_user_active(uid)
        return True

    async def enter_user_queue(self, user_id: int) -> None:
        await self.limiter.acquire()
        lock = self._locks[int(user_id)]
        await lock.acquire()

    async def leave_user_queue(self, user_id: int) -> None:
        lock = self._locks[int(user_id)]
        if lock.locked():
            lock.release()

    async def ensure_admin(self, update: Update) -> bool:
        user = update.effective_user
        if not user:
            return False
        if not self.config.is_admin(int(user.id)):
            if update.effective_message:
                await update.effective_message.reply_text("🔒 Admin command only.")
            return False
        return True
