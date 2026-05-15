"""Project file management with safe path handling and ZIP export."""

from __future__ import annotations

import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import aiofiles

from security import SecurityManager
from utils import ensure_dir


class FileManager:
    """Manages per-user project files in a secure root directory."""

    def __init__(self, root_dir: str, security: SecurityManager) -> None:
        self.root_dir = Path(root_dir)
        self.projects_dir = self.root_dir / "projects"
        self.tmp_dir = self.root_dir / "tmp"
        self.security = security
        ensure_dir(str(self.projects_dir))
        ensure_dir(str(self.tmp_dir))

    def user_project_dir(self, user_id: int, project_name: str = "default") -> Path:
        safe_project = "".join(ch for ch in project_name if ch.isalnum() or ch in "-_ ").strip() or "default"
        project_dir = self.projects_dir / str(user_id) / safe_project
        ensure_dir(str(project_dir))
        ensure_dir(str(project_dir / ".versions"))
        return project_dir

    async def write_file(self, user_id: int, relative_path: str, content: str, project_name: str = "default") -> str:
        project_dir = self.user_project_dir(user_id, project_name)
        target = self.security.secure_join(str(project_dir), relative_path)
        ensure_dir(os.path.dirname(target))

        if os.path.exists(target):
            await self._snapshot(target, project_dir)

        async with aiofiles.open(target, "w", encoding="utf-8") as handle:
            await handle.write(content)
        return target

    async def read_file(self, user_id: int, relative_path: str, project_name: str = "default") -> str:
        project_dir = self.user_project_dir(user_id, project_name)
        target = self.security.secure_join(str(project_dir), relative_path)
        async with aiofiles.open(target, "r", encoding="utf-8") as handle:
            return await handle.read()

    def list_files(self, user_id: int, project_name: str = "default") -> list[str]:
        project_dir = self.user_project_dir(user_id, project_name)
        files: list[str] = []
        for root, _, names in os.walk(project_dir):
            for name in names:
                full_path = os.path.join(root, name)
                rel = os.path.relpath(full_path, project_dir)
                if rel.startswith(".versions"):
                    continue
                files.append(rel)
        return sorted(files)

    def delete_file(self, user_id: int, relative_path: str, project_name: str = "default") -> bool:
        project_dir = self.user_project_dir(user_id, project_name)
        target = self.security.secure_join(str(project_dir), relative_path)
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

    def export_zip(self, user_id: int, project_name: str = "default") -> str:
        project_dir = self.user_project_dir(user_id, project_name)
        zip_path = self.tmp_dir / f"project_{user_id}_{project_name}.zip"

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for root, _, files in os.walk(project_dir):
                for file in files:
                    full = Path(root) / file
                    rel = full.relative_to(project_dir)
                    if str(rel).startswith(".versions"):
                        continue
                    archive.write(full, arcname=str(rel))
        return str(zip_path)

    async def _snapshot(self, filepath: str, project_dir: Path) -> None:
        rel = os.path.relpath(filepath, str(project_dir))
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        snapshot_name = rel.replace("/", "__") + f".{timestamp}.bak"
        snapshot_path = project_dir / ".versions" / snapshot_name
        ensure_dir(str(snapshot_path.parent))

        async with aiofiles.open(filepath, "r", encoding="utf-8") as src:
            data = await src.read()
        async with aiofiles.open(snapshot_path, "w", encoding="utf-8") as dst:
            await dst.write(data)
