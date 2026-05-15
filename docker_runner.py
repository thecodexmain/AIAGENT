"""Docker-based sandboxed command execution."""

from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import dataclass

from config_manager import ConfigManager
from security import SecurityManager


@dataclass
class RunResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


class DockerRunner:
    """Executes commands inside constrained temporary Docker containers."""

    def __init__(self, config: ConfigManager, security: SecurityManager) -> None:
        self.config = config
        self.security = security
        self.allowed_commands = {
            "python",
            "python3",
            "pip",
            "pip3",
            "pytest",
            "bash",
            "sh",
            "node",
            "npm",
            "go",
            "java",
            "javac",
        }

    async def run(self, project_dir: str, command: str) -> RunResult:
        safe_command = self.security.validate_command(command, self.allowed_commands)
        timeout = self.config.config.limits.docker_timeout_seconds
        mem_limit = self.config.config.limits.docker_memory_limit
        cpu_limit = self.config.config.limits.docker_cpu_limit

        if not os.path.isdir(project_dir):
            return RunResult(False, 1, "", "Project directory does not exist", False)

        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--cpus",
            str(cpu_limit),
            "--memory",
            str(mem_limit),
            "--pids-limit",
            "128",
            "-w",
            "/workspace",
            "-v",
            f"{project_dir}:/workspace:rw",
            "python:3.11-slim",
            "sh",
            "-lc",
            safe_command,
        ]

        process = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            return RunResult(
                ok=process.returncode == 0,
                exit_code=process.returncode or 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                timed_out=False,
            )
        except asyncio.TimeoutError:
            process.kill()
            return RunResult(
                ok=False,
                exit_code=124,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                timed_out=True,
            )
        except FileNotFoundError:
            return RunResult(
                ok=False,
                exit_code=127,
                stdout="",
                stderr="Docker is not installed or not available in PATH",
                timed_out=False,
            )

    @staticmethod
    def shell_join(command: list[str]) -> str:
        return " ".join(shlex.quote(part) for part in command)
