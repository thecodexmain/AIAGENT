"""Configuration loading and runtime updates for AIAGENT."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, HttpUrl, ValidationError

from security import SecurityManager
from utils import ensure_dir, merge_dict


class Limits(BaseModel):
    requests_per_minute: int = 20
    cooldown_seconds: int = 2
    max_prompt_chars: int = 12000
    max_file_size_mb: int = 5
    docker_timeout_seconds: int = 90
    docker_memory_limit: str = "512m"
    docker_cpu_limit: str = "1.0"


class RuntimeConfig(BaseModel):
    admin_ids: list[int] = Field(default_factory=list)
    default_model: str = "gpt-5.5"
    default_base_url: HttpUrl | str = "https://api.freemodel.dev/v1"
    maintenance_mode: bool = False
    limits: Limits = Field(default_factory=Limits)


DEFAULT_CONFIG = {
    "admin_ids": [],
    "default_model": "gpt-5.5",
    "default_base_url": "https://api.freemodel.dev/v1",
    "maintenance_mode": False,
    "limits": {
        "requests_per_minute": 20,
        "cooldown_seconds": 2,
        "max_prompt_chars": 12000,
        "max_file_size_mb": 5,
        "docker_timeout_seconds": 90,
        "docker_memory_limit": "512m",
        "docker_cpu_limit": "1.0",
    },
}


class ConfigManager:
    """Handles environment and JSON runtime configuration."""

    def __init__(self, root_dir: str) -> None:
        self.root_dir = Path(root_dir)
        self.config_path = self.root_dir / "config.json"
        self.env_path = self.root_dir / ".env"
        self.data_dir = self.root_dir / "data"
        self.key_file = self.data_dir / "api_key.enc"
        ensure_dir(str(self.data_dir))

        load_dotenv(self.env_path)
        self.config = self._load_config_file()

        seed = (
            os.getenv("APP_ENCRYPTION_SECRET")
            or os.getenv("TELEGRAM_BOT_TOKEN")
            or "change-this-seed-in-production"
        )
        self.security = SecurityManager(seed)

    def _load_config_file(self) -> RuntimeConfig:
        raw: dict[str, Any] = {}
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        merged = merge_dict(DEFAULT_CONFIG, raw)
        try:
            return RuntimeConfig(**merged)
        except ValidationError as exc:
            raise RuntimeError(f"Invalid config.json: {exc}") from exc

    def save(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(self.config.model_dump(), handle, indent=2, ensure_ascii=False)

    def get_bot_token(self) -> str:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")
        return token

    def get_api_key(self) -> str:
        env_key = os.getenv("OPENAI_API_KEY", "").strip()
        if env_key:
            return env_key

        if not self.key_file.exists():
            raise RuntimeError("No API key configured. Use /setkey as admin.")

        encrypted = self.key_file.read_text(encoding="utf-8").strip()
        if not encrypted:
            raise RuntimeError("Encrypted API key file is empty")
        return self.security.decrypt(encrypted)

    def set_api_key(self, key: str) -> None:
        encrypted = self.security.encrypt(key.strip())
        self.key_file.write_text(encrypted, encoding="utf-8")

    def get_model(self) -> str:
        return self.config.default_model

    def set_model(self, model: str) -> None:
        self.config.default_model = model.strip()
        self.save()

    def get_base_url(self) -> str:
        return str(self.config.default_base_url)

    def set_base_url(self, base_url: str) -> None:
        self.config.default_base_url = base_url.strip()
        self.save()

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.config.admin_ids

    def add_admin(self, user_id: int) -> None:
        if user_id not in self.config.admin_ids:
            self.config.admin_ids.append(user_id)
            self.save()
