"""Shared utilities for logging, formatting, and system stats."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psutil


START_TIME = time.time()


def setup_logging(log_dir: str = "logs", level: str = "INFO") -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("aiagent")
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(os.path.join(log_dir, "aiagent.log"), encoding="utf-8")
    file_handler.setFormatter(formatter)

    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_progress_bar(percent: int, width: int = 20) -> str:
    clamped = max(0, min(100, percent))
    filled = int(width * (clamped / 100))
    return f"[{'█' * filled}{'░' * (width - filled)}] {clamped}%"


def safe_markdown(text: str) -> str:
    escape_chars = "_[]()~`>#+-=|{}.!"
    output = []
    for ch in text:
        if ch in escape_chars:
            output.append("\\" + ch)
        else:
            output.append(ch)
    return "".join(output)


def should_trigger_continue(text: str) -> bool:
    normalized = (text or "").strip().lower()
    return normalized in {"continue", "/continue"}


def summarize_saved_files(files: list[str], max_items: int = 20) -> str:
    if not files:
        return "(no files saved)"
    head = files[:max_items]
    lines = [f"- {name}" for name in head]
    if len(files) > max_items:
        lines.append(f"- ... and {len(files) - max_items} more")
    return "\n".join(lines)


@dataclass
class SystemStats:
    cpu_percent: float
    ram_percent: float
    disk_percent: float
    uptime_seconds: float


def get_system_stats() -> SystemStats:
    return SystemStats(
        cpu_percent=psutil.cpu_percent(interval=0.2),
        ram_percent=psutil.virtual_memory().percent,
        disk_percent=psutil.disk_usage("/").percent,
        uptime_seconds=time.time() - START_TIME,
    )


def format_stats_message(stats: SystemStats, active_users: int, api_calls: int, error_count: int) -> str:
    return (
        "📊 *System Status*\n"
        f"• CPU: {stats.cpu_percent:.1f}%\n"
        f"• RAM: {stats.ram_percent:.1f}%\n"
        f"• Disk: {stats.disk_percent:.1f}%\n"
        f"• Uptime: {int(stats.uptime_seconds)}s\n"
        f"• Active Users: {active_users}\n"
        f"• API Calls: {api_calls}\n"
        f"• Errors: {error_count}"
    )


def clamp_text(text: str, limit: int = 3900) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 24] + "\n\n...[truncated by bot]"


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dict(result[key], value)
        else:
            result[key] = value
    return result
