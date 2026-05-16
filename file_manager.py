"""Project file management with safe path handling, size limits, and ZIP export."""

from __future__ import annotations

import os
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiofiles

from security import SecurityError, SecurityManager
from utils import ensure_dir


@dataclass
class SavedFile:
    path: str
    size_bytes: int
    language: str = ""


class FileManager:
    """Manages per-user project files in a secure root directory."""

    _MAX_RELATIVE_PATH = 260
    _BLOCKED_PARTS = {".versions", ".git", "__pycache__"}
    _BLOCKED_FILENAMES = {".env", "id_rsa", "authorized_keys"}

    def __init__(self, root_dir: str, security: SecurityManager, max_file_size_mb: int = 5) -> None:
        self.root_dir = Path(root_dir)
        self.projects_dir = self.root_dir / "projects"
        self.tmp_dir = self.root_dir / "tmp"
        self.security = security
        if int(max_file_size_mb) <= 0:
            raise ValueError("max_file_size_mb must be a positive number")
        self.max_file_size_bytes = int(max_file_size_mb) * 1024 * 1024
        ensure_dir(str(self.projects_dir))
        ensure_dir(str(self.tmp_dir))

    def user_project_dir(self, user_id: int, project_name: str = "default") -> Path:
        safe_project = "".join(ch for ch in project_name if ch.isalnum() or ch in "-_ ").strip() or "default"
        project_dir = self.projects_dir / str(user_id) / safe_project
        ensure_dir(str(project_dir))
        ensure_dir(str(project_dir / ".versions"))
        return project_dir

    def _validate_relative_path(self, relative_path: str) -> str:
        candidate = (relative_path or "").strip().replace("\\", "/")
        if not candidate:
            raise SecurityError("Filename cannot be empty")
        if len(candidate) > self._MAX_RELATIVE_PATH:
            raise SecurityError("Filename is too long")
        if "\x00" in candidate:
            raise SecurityError("Filename contains null bytes")
        if candidate.startswith("/"):
            raise SecurityError("Absolute paths are not allowed")

        normalized = os.path.normpath(candidate).replace("\\", "/")
        if normalized in {".", ".."} or normalized.startswith("../"):
            raise SecurityError("Path traversal attempt blocked")

        parts = [part for part in normalized.split("/") if part]
        if not parts:
            raise SecurityError("Invalid filename")

        for part in parts:
            if part in {".", ".."}:
                raise SecurityError("Path traversal attempt blocked")
            if part in self._BLOCKED_PARTS:
                raise SecurityError(f"Writing to protected directory '{part}' is blocked")

        filename = parts[-1]
        if filename.lower() in self._BLOCKED_FILENAMES:
            raise SecurityError(f"Writing to protected file '{filename}' is blocked")

        return normalized

    async def write_file(self, user_id: int, relative_path: str, content: str, project_name: str = "default") -> str:
        project_dir = self.user_project_dir(user_id, project_name)
        safe_relative = self._validate_relative_path(relative_path)
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > self.max_file_size_bytes:
            raise SecurityError(
                f"File '{safe_relative}' exceeds size limit of {self.max_file_size_bytes // (1024 * 1024)} MB"
            )

        target = self.security.secure_join(str(project_dir), safe_relative)
        ensure_dir(os.path.dirname(target))

        if os.path.exists(target):
            await self._snapshot(target, project_dir)

        async with aiofiles.open(target, "w", encoding="utf-8") as handle:
            await handle.write(content)
        return safe_relative

    async def write_files(
        self,
        user_id: int,
        files: list[tuple[str, str, str]],
        project_name: str = "default",
    ) -> list[SavedFile]:
        saved: list[SavedFile] = []
        for relative_path, content, language in files:
            rel = await self.write_file(user_id, relative_path, content, project_name=project_name)
            saved.append(SavedFile(path=rel, size_bytes=len(content.encode("utf-8")), language=language))
        return saved

    async def read_file(self, user_id: int, relative_path: str, project_name: str = "default") -> str:
        project_dir = self.user_project_dir(user_id, project_name)
        safe_relative = self._validate_relative_path(relative_path)
        target = self.security.secure_join(str(project_dir), safe_relative)
        async with aiofiles.open(target, "r", encoding="utf-8") as handle:
            return await handle.read()

    def user_file_path(self, user_id: int, relative_path: str, project_name: str = "default") -> Path:
        project_dir = self.user_project_dir(user_id, project_name)
        safe_relative = self._validate_relative_path(relative_path)
        target = self.security.secure_join(str(project_dir), safe_relative)
        return Path(target)

    def list_files(self, user_id: int, project_name: str = "default") -> list[str]:
        project_dir = self.user_project_dir(user_id, project_name)
        files: list[str] = []
        for root, _, names in os.walk(project_dir):
            for name in names:
                full_path = os.path.join(root, name)
                rel = os.path.relpath(full_path, project_dir)
                normalized = rel.replace("\\", "/")
                if normalized.startswith(".versions"):
                    continue
                files.append(normalized)
        return sorted(files)

    def delete_file(self, user_id: int, relative_path: str, project_name: str = "default") -> bool:
        project_dir = self.user_project_dir(user_id, project_name)
        safe_relative = self._validate_relative_path(relative_path)
        target = self.security.secure_join(str(project_dir), safe_relative)
        if os.path.isfile(target):
            os.remove(target)
            return True
        return False

    def delete_project(self, user_id: int, project_name: str = "default") -> bool:
        project_dir = self.user_project_dir(user_id, project_name)
        if project_dir.exists():
            shutil.rmtree(project_dir)
            return True
        return False

    def list_user_projects(self, user_id: int) -> list[str]:
        base = self.projects_dir / str(user_id)
        if not base.exists():
            return []
        return sorted([entry.name for entry in base.iterdir() if entry.is_dir()])

    def export_zip(self, user_id: int, project_name: str = "default", filename: str | None = None) -> str:
        project_dir = self.user_project_dir(user_id, project_name)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        default_name = f"project_{user_id}_{project_name}_{timestamp}.zip"
        safe_name = (filename or default_name).strip().replace("\\", "/")
        if "/" in safe_name:
            safe_name = Path(safe_name).name
        if not safe_name.lower().endswith(".zip"):
            safe_name = f"{safe_name}.zip"
        zip_path = self.tmp_dir / safe_name

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for root, _, files in os.walk(project_dir):
                for file in files:
                    full = Path(root) / file
                    rel = full.relative_to(project_dir)
                    rel_str = str(rel).replace("\\", "/")
                    if rel_str.startswith(".versions"):
                        continue
                    archive.write(full, arcname=rel_str)
        return str(zip_path)

    async def _snapshot(self, filepath: str, project_dir: Path) -> None:
        rel = os.path.relpath(filepath, str(project_dir))
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        snapshot_name = rel.replace("/", "__") + f".{timestamp}.bak"
        snapshot_path = project_dir / ".versions" / snapshot_name
        ensure_dir(str(snapshot_path.parent))

        async with aiofiles.open(filepath, "r", encoding="utf-8") as src:
            data = await src.read()
        async with aiofiles.open(snapshot_path, "w", encoding="utf-8") as dst:
            await dst.write(data)
