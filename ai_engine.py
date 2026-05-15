"""OpenAI-compatible async AI engine with streaming and file extraction."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import AsyncGenerator

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config_manager import ConfigManager
from memory import MemoryManager


class AIEngineError(Exception):
    """Raised for AI provider communication issues."""


@dataclass
class AIResponse:
    text: str
    files: dict[str, str]


FILE_BLOCK_RE = re.compile(
    r"FILE:\s*(?P<path>[^\n]+)\n```(?:[\w.+-]+)?\n(?P<content>[\s\S]*?)```",
    re.MULTILINE,
)


class AIEngine:
    """Handles model communication, retries, and structured extraction."""

    def __init__(self, config: ConfigManager, memory: MemoryManager) -> None:
        self.config = config
        self.memory = memory

    async def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.get_api_key()}",
            "Content-Type": "application/json",
        }

    @retry(wait=wait_exponential(multiplier=1, min=1, max=8), stop=stop_after_attempt(3), reraise=True)
    async def ask(self, user_id: int, prompt: str, system_prompt: str = "You are an expert coding assistant.") -> AIResponse:
        recent = await self.memory.get_recent_history(user_id, limit=20)
        messages = [{"role": "system", "content": system_prompt}]
        for item in recent:
            messages.append({"role": item.get("role", "user"), "content": item.get("content", "")})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.config.get_model(),
            "messages": messages,
            "temperature": 0.2,
            "stream": False,
        }

        url = f"{self.config.get_base_url().rstrip('/')}/chat/completions"
        headers = await self._headers()

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code >= 400:
                raise AIEngineError(f"AI API error {response.status_code}: {response.text}")
            data = response.json()

        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        files = self.extract_files(content)

        await self.memory.append_history(user_id, "user", prompt)
        await self.memory.append_history(user_id, "assistant", content)
        await self.memory.increment_api_calls()

        return AIResponse(text=content, files=files)

    async def stream(
        self,
        user_id: int,
        prompt: str,
        system_prompt: str = "You are an expert coding assistant.",
    ) -> AsyncGenerator[str, None]:
        recent = await self.memory.get_recent_history(user_id, limit=20)
        messages = [{"role": "system", "content": system_prompt}]
        for item in recent:
            messages.append({"role": item.get("role", "user"), "content": item.get("content", "")})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.config.get_model(),
            "messages": messages,
            "temperature": 0.2,
            "stream": True,
        }

        url = f"{self.config.get_base_url().rstrip('/')}/chat/completions"
        headers = await self._headers()
        final_text_parts: list[str] = []

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise AIEngineError(f"AI API error {response.status_code}: {body.decode('utf-8', 'replace')}")

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line.replace("data:", "", 1).strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if delta:
                        final_text_parts.append(delta)
                        yield delta

        final_text = "".join(final_text_parts).strip()
        await self.memory.append_history(user_id, "user", prompt)
        await self.memory.append_history(user_id, "assistant", final_text)
        await self.memory.increment_api_calls()

    @staticmethod
    def extract_files(text: str) -> dict[str, str]:
        files: dict[str, str] = {}
        for match in FILE_BLOCK_RE.finditer(text):
            path = match.group("path").strip()
            content = match.group("content")
            if path:
                files[path] = content
        return files
