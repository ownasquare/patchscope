from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest

from patchscope.analyzers import process as process_module
from patchscope.analyzers.base import AnalyzerStatus
from patchscope.analyzers.process import FixedCommandRunner


class FakeProcess:
    def __init__(self, *, returncode: int | None = 0, wait_error: Exception | None = None) -> None:
        self.pid = 4242
        self.returncode = returncode
        self.wait_error = wait_error
        self.wait_calls = 0

    def wait(self, timeout: float) -> int | None:
        del timeout
        self.wait_calls += 1
        if self.wait_error is not None:
            raise self.wait_error
        return self.returncode


def _resolved_runner(monkeypatch: pytest.MonkeyPatch, *, max_output_bytes: int = 2_000_000):
    runner = FixedCommandRunner(max_output_bytes=max_output_bytes)
    monkeypatch.setattr(
        FixedCommandRunner,
        "_resolve_executable",
        staticmethod(lambda _executable: "/trusted/analyzer"),
    )
    return runner


@pytest.mark.parametrize("max_output_bytes", [0, -1])
def test_runner_rejects_nonpositive_output_limit(max_output_bytes: int) -> None:
    with pytest.raises(ValueError, match="max_output_bytes must be positive"):
        FixedCommandRunner(max_output_bytes=max_output_bytes)


def test_run_rejects_invalid_timeout_before_launch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        FixedCommandRunner().run("ruff", (), cwd=tmp_path, timeout_seconds=0)


def test_run_returns_unavailable_without_spawning(tmp_path: Path) -> None:
    result = FixedCommandRunner().run(
        "patchscope-missing-analyzer",
        ("check",),
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert result.status is AnalyzerStatus.UNAVAILABLE
    assert result.argv == ("patchscope-missing-analyzer", "check")
    assert result.duration_ms == 0
    assert "not installed" in result.message


@pytest.mark.parametrize(
    ("error", "expected_status", "message"),
    [
        (FileNotFoundError("private path"), AnalyzerStatus.UNAVAILABLE, "unavailable"),
        (PermissionError("private path"), AnalyzerStatus.UNAVAILABLE, "unavailable"),
        (OSError("private path"), AnalyzerStatus.ERROR, "could not be started"),
    ],
)
def test_run_classifies_process_launch_failures_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: OSError,
    expected_status: AnalyzerStatus,
    message: str,
) -> None:
    runner = _resolved_runner(monkeypatch)

    def fail_to_start(*_args: Any, **_kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(process_module.subprocess, "Popen", fail_to_start)

    result = runner.run("ruff", ("check",), cwd=tmp_path, timeout_seconds=1)

    assert result.status is expected_status
    assert result.argv == ("ruff", "check")
    assert message in result.message
    assert str(error) not in result.message


def test_run_uses_bounded_output_and_a_minimal_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _resolved_runner(monkeypatch, max_output_bytes=4)
    captured: dict[str, Any] = {}

    def fake_popen(argv: tuple[str, ...], **kwargs: Any) -> FakeProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        kwargs["stdout"].write(b"abcdef")
        kwargs["stderr"].write(b"\xffboom")
        return FakeProcess(returncode=7)

    monkeypatch.setattr(process_module.subprocess, "Popen", fake_popen)

    result = runner.run("ruff", ("check",), cwd=tmp_path, timeout_seconds=2)

    assert result.status is AnalyzerStatus.SUCCEEDED
    assert result.exit_code == 7
    assert result.stdout == "abcd\n[output truncated]"
    assert result.stderr.endswith("\n[output truncated]")
    assert "\ufffd" in result.stderr
    assert captured["argv"] == ("/trusted/analyzer", "check")
    assert captured["shell"] is False
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["start_new_session"] is (os.name == "posix")
    assert captured["close_fds"] is True
    assert set(captured["env"]) == {
        "HOME",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONIOENCODING",
    }
    assert captured["env"]["HOME"] == str(tmp_path.resolve())


def test_run_marks_timeout_and_preserves_partial_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _resolved_runner(monkeypatch)
    process = FakeProcess(
        returncode=None,
        wait_error=subprocess.TimeoutExpired(cmd="ruff", timeout=0.01),
    )
    terminated: list[int] = []

    def fake_popen(_argv: tuple[str, ...], **kwargs: Any) -> FakeProcess:
        kwargs["stdout"].write(b"partial output")
        kwargs["stderr"].write(b"still working")
        return process

    def fake_terminate(value: FakeProcess) -> None:
        value.returncode = -signal.SIGKILL
        terminated.append(value.pid)

    monkeypatch.setattr(process_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(FixedCommandRunner, "_terminate", staticmethod(fake_terminate))

    result = runner.run("ruff", ("check",), cwd=tmp_path, timeout_seconds=0.01)

    assert result.status is AnalyzerStatus.TIMEOUT
    assert result.exit_code == -signal.SIGKILL
    assert result.stdout == "partial output"
    assert result.stderr == "still working"
    assert terminated == [4242]
    assert "time limit" in result.message


def test_run_rejects_file_as_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _resolved_runner(monkeypatch)
    file_path = tmp_path / "not-a-directory"
    file_path.write_text("content", encoding="utf-8")

    with pytest.raises(ValueError, match="cwd must be a directory"):
        runner.run("ruff", (), cwd=file_path, timeout_seconds=1)


def test_resolve_executable_accepts_only_safe_names_or_executable_absolute_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "trusted-tool"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)

    assert FixedCommandRunner._resolve_executable(str(executable)) == str(executable.resolve())
    assert FixedCommandRunner._resolve_executable(str(tmp_path / "missing")) is None

    monkeypatch.setattr(process_module.shutil, "which", lambda name: f"/trusted/{name}")
    assert FixedCommandRunner._resolve_executable("ruff") == "/trusted/ruff"

    with pytest.raises(ValueError, match="non-empty string"):
        FixedCommandRunner._resolve_executable("")
    with pytest.raises(ValueError, match="simple name"):
        FixedCommandRunner._resolve_executable("../ruff")


@pytest.mark.parametrize(
    "arguments",
    [
        ["check"],
        tuple("value" for _ in range(2_001)),
        ("bad\x00value",),
        ("x" * 10_001,),
        (1,),
    ],
)
def test_validate_arguments_rejects_unsafe_or_unbounded_values(arguments: object) -> None:
    expected = TypeError if isinstance(arguments, list) else ValueError
    with pytest.raises(expected):
        FixedCommandRunner._validate_arguments(arguments)  # type: ignore[arg-type]


def test_terminate_escalates_from_term_to_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []

    class EscalatingProcess(FakeProcess):
        def wait(self, timeout: float) -> int:
            del timeout
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(cmd="ruff", timeout=1)
            self.returncode = -signal.SIGKILL
            return self.returncode

    process = EscalatingProcess(returncode=None)
    monkeypatch.setattr(
        process_module.os,
        "killpg",
        lambda _pid, sent_signal: signals.append(sent_signal),
    )

    FixedCommandRunner._terminate(process)  # type: ignore[arg-type]

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.wait_calls == 2


def test_terminate_tolerates_an_already_gone_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(returncode=-signal.SIGTERM)
    calls = 0

    def missing_process(_pid: int, _signal: int) -> None:
        nonlocal calls
        calls += 1
        raise ProcessLookupError

    monkeypatch.setattr(process_module.os, "killpg", missing_process)

    FixedCommandRunner._terminate(process)  # type: ignore[arg-type]

    assert calls == 2
    assert process.wait_calls == 1


def test_terminate_uses_process_methods_on_non_posix_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NonPosixProcess(FakeProcess):
        def __init__(self) -> None:
            super().__init__(returncode=None)
            self.terminated = False
            self.killed = False

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float) -> int:
            del timeout
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(cmd="ruff", timeout=1)
            self.returncode = -1
            return self.returncode

    process = NonPosixProcess()
    monkeypatch.setattr(process_module.os, "name", "nt")

    FixedCommandRunner._terminate(process)  # type: ignore[arg-type]

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 2
