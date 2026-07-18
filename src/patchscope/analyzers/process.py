"""Fixed-argument, bounded subprocess execution for trusted analyzer binaries."""

from __future__ import annotations

import os
import re
import shutil
import signal

# Only trusted analyzer binaries and fixed argument arrays reach this module.
import subprocess  # nosec B404
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from patchscope.analyzers.base import AnalyzerStatus

_EXECUTABLE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    status: AnalyzerStatus
    argv: tuple[str, ...]
    duration_ms: int
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    message: str = ""


class FixedCommandRunner:
    """Run an installed analyzer without a shell or inherited credential environment."""

    def __init__(self, *, max_output_bytes: int = 2_000_000) -> None:
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        self.max_output_bytes = max_output_bytes

    def run(
        self,
        executable: str,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> ProcessResult:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        resolved = self._resolve_executable(executable)
        display_argv = (Path(executable).name, *arguments)
        if resolved is None:
            return ProcessResult(
                status=AnalyzerStatus.UNAVAILABLE,
                argv=display_argv,
                duration_ms=0,
                message="The analyzer executable is not installed or is not executable.",
            )
        self._validate_arguments(arguments)
        resolved_cwd = cwd.resolve(strict=True)
        if not resolved_cwd.is_dir():
            raise ValueError("cwd must be a directory")
        argv = (resolved, *arguments)
        environment = {
            "HOME": str(resolved_cwd),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1",
            "PATH": os.environ.get("PATH", os.defpath),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        started = time.monotonic()
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(  # nosec B603  # noqa: S603
                    argv,
                    cwd=resolved_cwd,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                    start_new_session=os.name == "posix",
                    close_fds=True,
                )
            except (FileNotFoundError, PermissionError):
                return ProcessResult(
                    status=AnalyzerStatus.UNAVAILABLE,
                    argv=display_argv,
                    duration_ms=_duration_ms(started),
                    message="The analyzer executable is unavailable.",
                )
            except OSError:
                return ProcessResult(
                    status=AnalyzerStatus.ERROR,
                    argv=display_argv,
                    duration_ms=_duration_ms(started),
                    message="The analyzer process could not be started.",
                )
            timed_out = False
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(process)
            stdout = self._read_output(stdout_file)
            stderr = self._read_output(stderr_file)
        if timed_out:
            return ProcessResult(
                status=AnalyzerStatus.TIMEOUT,
                argv=display_argv,
                duration_ms=_duration_ms(started),
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode,
                message="The analyzer exceeded its time limit and was stopped.",
            )
        return ProcessResult(
            status=AnalyzerStatus.SUCCEEDED,
            argv=display_argv,
            duration_ms=_duration_ms(started),
            stdout=stdout,
            stderr=stderr,
            exit_code=process.returncode,
        )

    @staticmethod
    def _resolve_executable(executable: str) -> str | None:
        if not isinstance(executable, str) or not executable:
            raise ValueError("executable must be a non-empty string")
        candidate = Path(executable)
        if candidate.is_absolute():
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
            return None
        if not _EXECUTABLE_RE.fullmatch(executable):
            raise ValueError("executable must be a simple name or absolute path")
        return shutil.which(executable)

    @staticmethod
    def _validate_arguments(arguments: tuple[str, ...]) -> None:
        if not isinstance(arguments, tuple):
            raise TypeError("analyzer arguments must be a tuple")
        if len(arguments) > 2_000:
            raise ValueError("too many analyzer arguments")
        for argument in arguments:
            if not isinstance(argument, str) or "\x00" in argument or len(argument) > 10_000:
                raise ValueError("analyzer argument is invalid")

    def _read_output(self, stream: BinaryIO) -> str:
        stream.seek(0)
        raw = stream.read(self.max_output_bytes + 1)
        truncated = len(raw) > self.max_output_bytes
        raw = raw[: self.max_output_bytes]
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            return f"{text}\n[output truncated]"
        return text

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=1.0)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=1.0)


def _duration_ms(started: float) -> int:
    return max(int((time.monotonic() - started) * 1_000), 0)


__all__ = ["FixedCommandRunner", "ProcessResult"]
