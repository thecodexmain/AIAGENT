"""Security helpers: encryption, path safety, command validation, and audit logging."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from typing import Iterable

from cryptography.fernet import Fernet

from utils import ensure_dir, utc_now_iso


class SecurityError(Exception):
    """Raised for security policy violations."""


class SecurityManager:
    """Encapsulates encryption, input validation, and audit logging."""

    _DISALLOWED_PATTERNS = [
        r"\$\(",
        r"`",
        r"\|\|",
        r"&&",
        r";",
        r">",
        r"<",
        r"\n",
    ]

    def __init__(self, secret_seed: str, audit_log_file: str = "logs/audit.log") -> None:
        if not secret_seed:
            raise ValueError("A non-empty secret seed is required")
        self._fernet = Fernet(self._derive_key(secret_seed))
        self.audit_log_file = audit_log_file
        ensure_dir(os.path.dirname(audit_log_file) or ".")

    @staticmethod
    def _derive_key(seed: str) -> bytes:
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")

    def secure_join(self, root: str, relative_path: str) -> str:
        if not relative_path or relative_path.strip() in {".", "/"}:
            raise SecurityError("Invalid relative path")
        normalized = os.path.normpath(relative_path).replace("\\", "/")
        if normalized.startswith("../") or normalized == "..":
            raise SecurityError("Path traversal attempt blocked")
        abs_root = os.path.abspath(root)
        abs_path = os.path.abspath(os.path.join(abs_root, normalized))
        if not abs_path.startswith(abs_root + os.sep) and abs_path != abs_root:
            raise SecurityError("Resolved path escaped root directory")
        return abs_path

    def validate_command(self, command: str, allow_commands: Iterable[str]) -> str:
        clean = command.strip()
        if not clean:
            raise SecurityError("Command cannot be empty")

        first_token = clean.split()[0]
        if first_token not in set(allow_commands):
            raise SecurityError(f"Command '{first_token}' is not allowed")

        for pattern in self._DISALLOWED_PATTERNS:
            if re.search(pattern, clean):
                raise SecurityError("Potentially unsafe command blocked")
        return clean

    def validate_user_text(self, text: str, max_length: int = 12000) -> str:
        stripped = text.strip()
        if not stripped:
            raise SecurityError("Text cannot be empty")
        if len(stripped) > max_length:
            raise SecurityError("Text exceeds maximum allowed length")
        return stripped

    def audit(self, event: str, actor: str, details: dict) -> None:
        payload = {
            "timestamp": utc_now_iso(),
            "event": event,
            "actor": actor,
            "details": details,
        }
        with open(self.audit_log_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
