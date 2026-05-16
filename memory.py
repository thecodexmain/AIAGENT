"""Persistent user session, stats, and ChatGPT-style chat memory manager."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiofiles

from utils import ensure_dir, utc_now_iso


class MemoryManager:
    """Stores sessions, stats, and multi-chat conversation history."""

    def __init__(self, root_dir: str) -> None:
        self.root_dir = Path(root_dir)
        self.sessions_dir = self.root_dir / "data" / "sessions"
        self.history_dir = self.root_dir / "data" / "history"  # legacy compatibility
        self.chats_dir = self.root_dir / "data" / "chats"
        self.stats_file = self.root_dir / "data" / "stats.json"
        self._lock = asyncio.Lock()

        ensure_dir(str(self.sessions_dir))
        ensure_dir(str(self.history_dir))
        ensure_dir(str(self.chats_dir))
        ensure_dir(str(self.stats_file.parent))

        if not self.stats_file.exists():
            self.stats_file.write_text(
                json.dumps({"api_calls": 0, "errors": 0, "active_users": []}, indent=2),
                encoding="utf-8",
            )

    def _session_path(self, user_id: int) -> Path:
        return self.sessions_dir / f"{user_id}.json"

    def _legacy_history_path(self, user_id: int) -> Path:
        return self.history_dir / f"{user_id}.jsonl"

    def _user_chat_dir(self, user_id: int) -> Path:
        return self.chats_dir / str(user_id)

    def _chat_index_path(self, user_id: int) -> Path:
        return self._user_chat_dir(user_id) / "index.json"

    def _chat_messages_path(self, user_id: int, chat_id: str) -> Path:
        return self._user_chat_dir(user_id) / f"{chat_id}.jsonl"

    @staticmethod
    def _generate_chat_id() -> str:
        return f"chat_{uuid4().hex[:12]}"

    async def _read_json_file(self, path: Path, fallback: Any) -> Any:
        if not path.exists():
            return fallback
        async with aiofiles.open(path, "r", encoding="utf-8") as handle:
            text = await handle.read()
        if not text.strip():
            return fallback
        return json.loads(text)

    async def _write_json_file(self, path: Path, payload: Any) -> None:
        ensure_dir(str(path.parent))
        async with aiofiles.open(path, "w", encoding="utf-8") as handle:
            await handle.write(json.dumps(payload, ensure_ascii=False, indent=2))

    async def _load_index(self, user_id: int) -> dict[str, Any]:
        ensure_dir(str(self._user_chat_dir(user_id)))
        index = await self._read_json_file(
            self._chat_index_path(user_id),
            {"active_chat_id": "", "chats": []},
        )
        if not isinstance(index, dict):
            return {"active_chat_id": "", "chats": []}
        if not isinstance(index.get("chats"), list):
            index["chats"] = []
        if not isinstance(index.get("active_chat_id"), str):
            index["active_chat_id"] = ""
        return index

    async def _save_index(self, user_id: int, index: dict[str, Any]) -> None:
        await self._write_json_file(self._chat_index_path(user_id), index)

    @staticmethod
    def _default_session() -> dict[str, Any]:
        return {
            "messages": [],
            "active_project": "default",
            "pending_task": "",
            "active_chat_id": "",
        }

    async def load_session(self, user_id: int) -> dict[str, Any]:
        path = self._session_path(user_id)
        if not path.exists():
            return self._default_session()
        async with aiofiles.open(path, "r", encoding="utf-8") as handle:
            text = await handle.read()
        payload = json.loads(text) if text.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        session = self._default_session()
        session.update(payload)
        return session

    async def save_session(self, user_id: int, payload: dict[str, Any]) -> None:
        path = self._session_path(user_id)
        session = self._default_session()
        session.update(payload)
        await self._write_json_file(path, session)

    async def _ensure_default_chat(self, user_id: int) -> dict[str, Any]:
        index = await self._load_index(user_id)
        chats: list[dict[str, Any]] = [chat for chat in index["chats"] if isinstance(chat, dict)]
        if chats:
            active = index.get("active_chat_id", "")
            if not active or active not in {str(chat.get("id", "")) for chat in chats}:
                index["active_chat_id"] = str(chats[0].get("id", ""))
                await self._save_index(user_id, index)
            return index

        # one-time migration from legacy single-history file
        chat_id = self._generate_chat_id()
        now = utc_now_iso()
        title = "Imported Chat" if self._legacy_history_path(user_id).exists() else "New Chat"
        chat_entry = {"id": chat_id, "title": title, "created_at": now, "updated_at": now}
        index["chats"] = [chat_entry]
        index["active_chat_id"] = chat_id
        await self._save_index(user_id, index)

        legacy = self._legacy_history_path(user_id)
        if legacy.exists():
            async with aiofiles.open(legacy, "r", encoding="utf-8") as handle:
                lines = (await handle.read()).splitlines()
            if lines:
                target = self._chat_messages_path(user_id, chat_id)
                async with aiofiles.open(target, "a", encoding="utf-8") as out:
                    for line in lines:
                        if line.strip():
                            await out.write(line + "\n")
        return index

    async def create_chat(self, user_id: int, title: str = "New Chat", set_active: bool = True) -> dict[str, Any]:
        async with self._lock:
            index = await self._load_index(user_id)
            chats = [chat for chat in index.get("chats", []) if isinstance(chat, dict)]
            index["chats"] = chats
            chat_id = self._generate_chat_id()
            now = utc_now_iso()
            entry = {"id": chat_id, "title": (title or "New Chat").strip()[:80], "created_at": now, "updated_at": now}
            index["chats"].insert(0, entry)
            if set_active:
                index["active_chat_id"] = chat_id
            await self._save_index(user_id, index)
            session = await self.load_session(user_id)
            session["active_chat_id"] = index.get("active_chat_id", chat_id)
            await self.save_session(user_id, session)
            return entry

    async def list_chats(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        async with self._lock:
            index = await self._ensure_default_chat(user_id)
            chats = list(index.get("chats", []))
        chats = [chat for chat in chats if isinstance(chat, dict)]
        chats.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return chats[: max(1, limit)]

    async def get_chat(self, user_id: int, chat_id: str) -> dict[str, Any] | None:
        chats = await self.list_chats(user_id, limit=500)
        for chat in chats:
            if str(chat.get("id", "")) == chat_id:
                return chat
        return None

    async def get_active_chat_id(self, user_id: int) -> str:
        async with self._lock:
            index = await self._ensure_default_chat(user_id)
            active_chat_id = str(index.get("active_chat_id", "")).strip()
            if not active_chat_id:
                active_chat_id = str(index.get("chats", [{}])[0].get("id", ""))
                index["active_chat_id"] = active_chat_id
                await self._save_index(user_id, index)
            session = await self.load_session(user_id)
            session["active_chat_id"] = active_chat_id
            await self.save_session(user_id, session)
            return active_chat_id

    async def set_active_chat(self, user_id: int, chat_id: str) -> bool:
        chat_id = (chat_id or "").strip()
        if not chat_id:
            return False
        async with self._lock:
            index = await self._ensure_default_chat(user_id)
            valid_ids = {str(chat.get("id", "")) for chat in index.get("chats", []) if isinstance(chat, dict)}
            if chat_id not in valid_ids:
                return False
            index["active_chat_id"] = chat_id
            await self._save_index(user_id, index)
            session = await self.load_session(user_id)
            session["active_chat_id"] = chat_id
            await self.save_session(user_id, session)
            return True

    async def rename_chat(self, user_id: int, chat_id: str, title: str) -> bool:
        value = (title or "").strip()
        if not value:
            return False
        async with self._lock:
            index = await self._ensure_default_chat(user_id)
            found = False
            now = utc_now_iso()
            for chat in index.get("chats", []):
                if str(chat.get("id", "")) == chat_id:
                    chat["title"] = value[:80]
                    chat["updated_at"] = now
                    found = True
                    break
            if not found:
                return False
            await self._save_index(user_id, index)
            return True

    async def delete_chat(self, user_id: int, chat_id: str) -> bool:
        async with self._lock:
            index = await self._ensure_default_chat(user_id)
            chats = [chat for chat in index.get("chats", []) if isinstance(chat, dict)]
            kept = [chat for chat in chats if str(chat.get("id", "")) != chat_id]
            if len(kept) == len(chats):
                return False
            index["chats"] = kept
            messages_path = self._chat_messages_path(user_id, chat_id)
            if messages_path.exists():
                os.remove(messages_path)
            if not kept:
                chat_id_new = self._generate_chat_id()
                now = utc_now_iso()
                entry = {"id": chat_id_new, "title": "New Chat", "created_at": now, "updated_at": now}
                kept = [entry]
                index["chats"] = kept
            active_id = str(index.get("active_chat_id", ""))
            valid = {str(chat.get("id", "")) for chat in kept}
            if active_id not in valid:
                index["active_chat_id"] = str(kept[0].get("id", ""))
            await self._save_index(user_id, index)
            session = await self.load_session(user_id)
            session["active_chat_id"] = index.get("active_chat_id", "")
            await self.save_session(user_id, session)
            return True

    async def touch_chat(self, user_id: int, chat_id: str) -> None:
        async with self._lock:
            index = await self._ensure_default_chat(user_id)
            now = utc_now_iso()
            for chat in index.get("chats", []):
                if str(chat.get("id", "")) == chat_id:
                    chat["updated_at"] = now
                    break
            await self._save_index(user_id, index)

    async def append_history(self, user_id: int, role: str, content: str, chat_id: str | None = None) -> None:
        cid = (chat_id or "").strip() or await self.get_active_chat_id(user_id)
        async with self._lock:
            entry = {"time": utc_now_iso(), "role": role, "content": content}
            path = self._chat_messages_path(user_id, cid)
            ensure_dir(str(path.parent))
            async with aiofiles.open(path, "a", encoding="utf-8") as handle:
                await handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            index = await self._ensure_default_chat(user_id)
            now = utc_now_iso()
            for chat in index.get("chats", []):
                if str(chat.get("id", "")) == cid:
                    chat["updated_at"] = now
                    break
            await self._save_index(user_id, index)

            # legacy compatibility mirror
            legacy_path = self._legacy_history_path(user_id)
            async with aiofiles.open(legacy_path, "a", encoding="utf-8") as handle:
                await handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def get_recent_history(self, user_id: int, limit: int = 30, chat_id: str | None = None) -> list[dict[str, Any]]:
        cid = (chat_id or "").strip() or await self.get_active_chat_id(user_id)
        path = self._chat_messages_path(user_id, cid)
        if not path.exists():
            return []
        async with aiofiles.open(path, "r", encoding="utf-8") as handle:
            lines = (await handle.read()).splitlines()
        entries: list[dict[str, Any]] = []
        for line in lines[-max(1, limit):]:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
        return entries

    async def reset_user(self, user_id: int) -> None:
        session_path = self._session_path(user_id)
        history_path = self._legacy_history_path(user_id)
        if session_path.exists():
            os.remove(session_path)
        if history_path.exists():
            os.remove(history_path)
        user_chat_dir = self._user_chat_dir(user_id)
        if user_chat_dir.exists():
            for entry in user_chat_dir.glob("*"):
                if entry.is_file():
                    entry.unlink()

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
