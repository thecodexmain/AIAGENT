"""Persistent user session and conversation memory manager."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import aiofiles

from utils import ensure_dir, utc_now_iso


class MemoryManager:
    """Stores and restores conversation history and active project state."""

    def __init__(self, root_dir: str) -> None:
        self.root_dir = Path(root_dir)
        self.sessions_dir = self.root_dir / "data" / "sessions"
        self.history_dir = self.root_dir / "data" / "history"
        self.stats_file = self.root_dir / "data" / "stats.json"
        self._lock = asyncio.Lock()

        ensure_dir(str(self.sessions_dir))
        ensure_dir(str(self.history_dir))
        ensure_dir(str(self.stats_file.parent))

        if not self.stats_file.exists():
            self.stats_file.write_text(
                json.dumps({"api_calls": 0, "errors": 0, "active_users": []}, indent=2),
                encoding="utf-8",
            )

    def _session_path(self, user_id: int) -> Path:
        return self.sessions_dir / f"{user_id}.json"

    def _history_path(self, user_id: int) -> Path:
        return self.history_dir / f"{user_id}.jsonl"

    async def load_session(self, user_id: int) -> dict[str, Any]:
        path = self._session_path(user_id)
        if not path.exists():
            return {"messages": [], "active_project": "default", "pending_task": ""}
        async with aiofiles.open(path, "r", encoding="utf-8") as handle:
            text = await handle.read()
            return json.loads(text)

    async def save_session(self, user_id: int, payload: dict[str, Any]) -> None:
        path = self._session_path(user_id)
        async with self._lock:
            async with aiofiles.open(path, "w", encoding="utf-8") as handle:
                await handle.write(json.dumps(payload, ensure_ascii=False, indent=2))

    async def append_history(self, user_id: int, role: str, content: str) -> None:
        entry = {"time": utc_now_iso(), "role": role, "content": content}
        path = self._history_path(user_id)
        async with self._lock:
            async with aiofiles.open(path, "a", encoding="utf-8") as handle:
                await handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def get_recent_history(self, user_id: int, limit: int = 30) -> list[dict[str, Any]]:
        path = self._history_path(user_id)
        if not path.exists():
            return []
        async with aiofiles.open(path, "r", encoding="utf-8") as handle:
            lines = (await handle.read()).splitlines()
        entries = [json.loads(line) for line in lines[-limit:]]
        return entries

    async def reset_user(self, user_id: int) -> None:
        session_path = self._session_path(user_id)
        history_path = self._history_path(user_id)
        if session_path.exists():
            os.remove(session_path)
        if history_path.exists():
            os.remove(history_path)

    async def _load_stats(self) -> dict[str, Any]:
        async with aiofiles.open(self.stats_file, "r", encoding="utf-8") as handle:
            return json.loads(await handle.read())

    async def _save_stats(self, stats: dict[str, Any]) -> None:
        async with aiofiles.open(self.stats_file, "w", encoding="utf-8") as handle:
            await handle.write(json.dumps(stats, indent=2, ensure_ascii=False))

    async def mark_user_active(self, user_id: int) -> None:
        async with self._lock:
            stats = await self._load_stats()
            users = set(stats.get("active_users", []))
            users.add(user_id)
            stats["active_users"] = sorted(users)
            await self._save_stats(stats)

    async def increment_api_calls(self) -> None:
        async with self._lock:
            stats = await self._load_stats()
            stats["api_calls"] = int(stats.get("api_calls", 0)) + 1
            await self._save_stats(stats)

    async def increment_errors(self) -> None:
        async with self._lock:
            stats = await self._load_stats()
            stats["errors"] = int(stats.get("errors", 0)) + 1
            await self._save_stats(stats)

    async def get_stats(self) -> dict[str, Any]:
        async with self._lock:
            return await self._load_stats()
