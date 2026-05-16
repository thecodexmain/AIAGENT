"""OpenAI-compatible async AI engine with robust streaming and file extraction."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, AsyncGenerator

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config_manager import ConfigManager
from memory import MemoryManager
from utils import normalize_whitespace


class AIEngineError(Exception):
    """Raised for AI provider communication issues."""


@dataclass
class ExtractedFile:
    path: str
    content: str
    language: str


@dataclass
class AIResponse:
    text: str
    files: dict[str, str]


@dataclass
class PromptEnhancement:
    original_prompt: str
    enhanced_prompt: str


FILE_BLOCK_RE = re.compile(
    r"FILE:\s*(?P<path>[^\r\n]+)\r?\n```(?P<lang>[\w.+-]*)\r?\n(?P<content>[\s\S]*?)\r?\n```",
    re.MULTILINE,
)
PROMPT_ENHANCEMENT_MAX_CHARS = 2800
PROMPT_ENHANCEMENT_SUFFIX = " ...[compressed]"
PROMPT_ENHANCEMENT_FALLBACK_DEFAULT_COUNT = 6

CONTEXT_TAG_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("login", "signup", "auth", "account", "password"),
        "secure authentication UX with robust validation and clear feedback",
    ),
    (
        ("dashboard", "admin", "panel", "analytics"),
        "data-dense dashboard layout with scalable navigation patterns",
    ),
    (
        ("form", "submit", "input", "checkout", "payment"),
        "high-conversion form UX with validation, helper text, and safe submission flows",
    ),
    (
        ("api", "backend", "server", "database"),
        "clean API contracts, error handling, and maintainable service architecture",
    ),
    (
        ("landing", "marketing", "portfolio", "home page", "homepage"),
        "premium visual storytelling with strong hierarchy and conversion-ready sections",
    ),
    (
        ("fix", "bug", "error", "issue", "crash"),
        "root-cause analysis, minimal-risk fixes, and regression-safe implementation",
    ),
    (
        ("refactor", "cleanup", "improve", "optimize"),
        "improved naming, structure, and performance with preserved behavior",
    ),
)


class AIEngine:
    """Handles model communication, retries, and structured extraction."""

    def __init__(self, config: ConfigManager, memory: MemoryManager) -> None:
        self.config = config
        self.memory = memory
        self.logger = logging.getLogger("aiagent")
        self._premium_defaults: tuple[str, ...] = (
            "modern UI/UX with clean typography and spacing consistency",
            "fully responsive behavior across mobile, tablet, and desktop",
            "smooth animations, transitions, and polished interaction states",
            "accessibility-first semantics and keyboard/screen-reader usability",
            "SVG-first icons, logos, and scalable vector illustrations",
            "reusable component architecture and cohesive design system",
            "loading, empty, error, and hover/focus states",
            "modern color palette with dark/light readiness when appropriate",
            "SEO fundamentals and front-end performance best practices",
            "production-ready structure, naming, and maintainable code quality",
        )

    async def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.get_api_key()}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                    continue
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                    continue
                if isinstance(part.get("content"), str):
                    parts.append(part["content"])
                    continue
                if isinstance(part.get("delta"), str):
                    parts.append(part["delta"])
            return "".join(parts)
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                return content["text"]
            if isinstance(content.get("content"), str):
                return content["content"]
        return ""

    @classmethod
    def _extract_delta_text(cls, choice: Any) -> str:
        if not isinstance(choice, dict):
            return ""

        delta = choice.get("delta")
        if isinstance(delta, dict):
            text = cls._content_to_text(delta.get("content"))
            if text:
                return text

        message = choice.get("message")
        if isinstance(message, dict):
            text = cls._content_to_text(message.get("content"))
            if text:
                return text

        if isinstance(choice.get("text"), str):
            return choice["text"]

        return ""

    @staticmethod
    def _safe_choices(payload: Any) -> list[Any]:
        if not isinstance(payload, dict):
            return []
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return []
        return [choice for choice in choices if choice is not None]

    @staticmethod
    def _infer_context_tags(prompt: str) -> list[str]:
        lower = prompt.lower()
        tags: list[str] = []
        for keywords, guidance in CONTEXT_TAG_RULES:
            if any(token in lower for token in keywords):
                tags.append(guidance)
        return tags

    def enhance_prompt(self, prompt: str, task_type: str = "build") -> PromptEnhancement:
        original = normalize_whitespace(prompt)
        base_prompt = original or "Build a complete production-ready software solution."
        inferred = self._infer_context_tags(base_prompt)
        task = (task_type or "build").strip().lower()
        task_focus = (
            "prioritize robust implementation quality, architecture, and polished product experience"
            if task == "build"
            else "prioritize debugging precision, safe fixes, and production-grade polish in touched areas"
        )
        quality_bar = (
            "Deliver a premium, production-grade result similar in quality expectations to top-tier AI coding agents. "
            "Fill missing requirements intelligently, remove ambiguity, and avoid basic template-level output."
        )
        premium_defaults = "; ".join(self._premium_defaults)
        inferred_text = "; ".join(inferred) if inferred else "context-aware architecture and UX enhancements inferred from intent"

        enhanced = (
            f"User intent: {base_prompt}. "
            f"Task mode: {task}. "
            f"{quality_bar} "
            f"Always include: {premium_defaults}. "
            f"Inferred requirements: {inferred_text}. "
            f"Execution focus: {task_focus}. "
            "Generate only required files, but ensure each file is polished, cohesive, and production-ready with no placeholders."
        )
        max_chars = PROMPT_ENHANCEMENT_MAX_CHARS
        if len(enhanced) > max_chars:
            concise_defaults = "; ".join(self._premium_defaults[:PROMPT_ENHANCEMENT_FALLBACK_DEFAULT_COUNT])
            enhanced = (
                f"User intent: {base_prompt}. Task mode: {task}. {quality_bar} "
                f"Always include: {concise_defaults}. "
                f"Inferred requirements: {inferred_text}. "
                f"Execution focus: {task_focus}. "
                "Generate only required files with production-ready quality and no placeholders."
            )
        if len(enhanced) > max_chars:
            suffix_budget = len(PROMPT_ENHANCEMENT_SUFFIX)
            enhanced = enhanced[: max_chars - suffix_budget].rstrip() + PROMPT_ENHANCEMENT_SUFFIX
        return PromptEnhancement(original_prompt=base_prompt, enhanced_prompt=enhanced)

    @retry(wait=wait_exponential(multiplier=1, min=1, max=8), stop=stop_after_attempt(3), reraise=True)
    async def ask(
        self,
        user_id: int,
        prompt: str,
        system_prompt: str = "You are an expert coding assistant.",
        chat_id: str | None = None,
    ) -> AIResponse:
        recent = await self.memory.get_recent_history(user_id, limit=20, chat_id=chat_id)
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
            try:
                data = response.json()
            except json.JSONDecodeError as exc:
                raise AIEngineError("AI API returned invalid JSON response") from exc

        content = ""
        for choice in self._safe_choices(data):
            content += self._extract_delta_text(choice)

        content = content.strip()
        files = self.extract_files(content)

        await self.memory.append_history(user_id, "user", prompt, chat_id=chat_id)
        await self.memory.append_history(user_id, "assistant", content, chat_id=chat_id)
        await self.memory.increment_api_calls()

        return AIResponse(text=content, files=files)

    async def stream(
        self,
        user_id: int,
        prompt: str,
        system_prompt: str = "You are an expert coding assistant.",
        chat_id: str | None = None,
    ) -> AsyncGenerator[str, None]:
        recent = await self.memory.get_recent_history(user_id, limit=20, chat_id=chat_id)
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
        yielded_any = False
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    async with client.stream("POST", url, headers=headers, json=payload) as response:
                        if response.status_code >= 400:
                            body = await response.aread()
                            raise AIEngineError(
                                f"AI API error {response.status_code}: {body.decode('utf-8', 'replace')}"
                            )

                        async for line in response.aiter_lines():
                            if not line:
                                continue

                            stripped = line.strip()
                            if not stripped.startswith("data:"):
                                continue

                            data_text = stripped[len("data:") :].strip()
                            if not data_text:
                                continue
                            if data_text == "[DONE]":
                                break

                            try:
                                chunk = json.loads(data_text)
                            except json.JSONDecodeError:
                                self.logger.warning(
                                    "Skipping malformed stream JSON chunk: %s",
                                    data_text[:160].replace("\n", "\\n"),
                                )
                                continue

                            if not isinstance(chunk, dict):
                                continue

                            for choice in self._safe_choices(chunk):
                                delta = self._extract_delta_text(choice)
                                if delta:
                                    final_text_parts.append(delta)
                                    yielded_any = True
                                    yield delta
                break
            except (httpx.HTTPError, asyncio.TimeoutError, AIEngineError) as exc:
                if attempt >= max_attempts or yielded_any:
                    raise AIEngineError(f"Streaming failed: {exc}") from exc
                wait_for = min(2**attempt, 5)
                self.logger.warning("Stream attempt %s failed, retrying in %ss", attempt, wait_for)
                await asyncio.sleep(wait_for)

        final_text = "".join(final_text_parts).strip()
        await self.memory.append_history(user_id, "user", prompt, chat_id=chat_id)
        await self.memory.append_history(user_id, "assistant", final_text, chat_id=chat_id)
        await self.memory.increment_api_calls()

    @staticmethod
    def extract_file_blocks(text: str) -> list[ExtractedFile]:
        files: list[ExtractedFile] = []
        for match in FILE_BLOCK_RE.finditer(text):
            path = match.group("path").strip()
            language = match.group("lang").strip()
            content = match.group("content")
            if path:
                files.append(ExtractedFile(path=path, content=content, language=language))
        return files

    @classmethod
    def extract_files(cls, text: str) -> dict[str, str]:
        files: dict[str, str] = {}
        for item in cls.extract_file_blocks(text):
            files[item.path] = item.content
        return files
