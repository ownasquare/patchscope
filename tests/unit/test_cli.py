from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from patchscope import __version__
from patchscope.cli import _stop_child, app
from patchscope.config import Settings


class RecordingService:
    def __init__(self) -> None:
        self.closed = False
        self.uploads: list[dict[str, object]] = []

    def review_upload(
        self,
        *,
        filename: str,
        content: bytes,
        title: str | None = None,
    ) -> dict[str, object]:
        self.uploads.append({"filename": filename, "content": content, "title": title})
        return {
            "id": "rev_cli",
            "summary": {"risk_score": 20, "recommendation": "request_changes"},
            "findings": [
                {
                    "severity": "high",
                    "category": "security",
                    "path": filename,
                    "start_line": 1,
                    "title": "Unsafe evaluation",
                }
            ],
        }

    def capabilities(self) -> dict[str, object]:
        return {
            "analyzers": [
                {"name": "ruff", "status": "available", "detail": "Installed."},
                {"name": "semgrep", "status": "unavailable", "detail": "Optional."},
            ]
        }

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_version_prints_public_version(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"PatchScope {__version__}"


def test_serve_passes_bounded_options_to_uvicorn(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, Any] = {}

    def fake_run(target: str, **kwargs: Any) -> None:
        recorded["target"] = target
        recorded.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)

    result = runner.invoke(
        app,
        ["serve", "--host", "127.0.0.3", "--port", "9000", "--reload"],
    )

    assert result.exit_code == 0
    assert recorded == {
        "target": "patchscope.api.app:app",
        "host": "127.0.0.3",
        "port": 9000,
        "reload": True,
    }


def test_ui_uses_literal_subprocess_arguments_and_propagates_exit_code(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []

    def fake_run(
        command: list[str], *, check: bool, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        commands.append(command)
        environments.append(env)
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setenv("PATCHSCOPE_OPENAI_API_KEY", "api-only")
    monkeypatch.setenv("PATCHSCOPE_GITHUB_TOKEN", "api-only")
    monkeypatch.setattr("patchscope.cli.subprocess.run", fake_run)

    result = runner.invoke(app, ["ui", "--host", "127.0.0.2", "--port", "8600"])

    assert result.exit_code == 7
    assert commands == [
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(Path(__file__).parents[2] / "src/patchscope/streamlit_app.py"),
            "--server.address",
            "127.0.0.2",
            "--server.port",
            "8600",
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ]
    ]
    assert "PATCHSCOPE_OPENAI_API_KEY" not in environments[0]
    assert "PATCHSCOPE_GITHUB_TOKEN" not in environments[0]


class FakeChildProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        return int(self.returncode or 0)


def test_start_launches_both_services_with_fixed_arguments_and_isolates_ui_secrets(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeChildProcess(returncode=9)
    ui = FakeChildProcess()
    children = iter([api, ui])
    launches: list[tuple[list[str], dict[str, str]]] = []

    def fake_popen(command: list[str], *, env: dict[str, str]) -> FakeChildProcess:
        launches.append((command, env))
        return next(children)

    monkeypatch.setenv("PATCHSCOPE_OPENAI_API_KEY", "api-only")
    monkeypatch.setenv("PATCHSCOPE_GITHUB_TOKEN", "api-only")
    monkeypatch.setattr("patchscope.cli.subprocess.Popen", fake_popen)

    result = runner.invoke(
        app,
        ["start", "--host", "127.0.0.2", "--api-port", "9000", "--ui-port", "8600"],
    )

    assert result.exit_code == 9
    assert launches[0][0] == [
        sys.executable,
        "-m",
        "uvicorn",
        "patchscope.api.app:app",
        "--host",
        "127.0.0.2",
        "--port",
        "9000",
    ]
    assert launches[1][0] == [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(Path(__file__).parents[2] / "src/patchscope/streamlit_app.py"),
        "--server.address",
        "127.0.0.2",
        "--server.port",
        "8600",
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    assert launches[0][1]["PATCHSCOPE_OPENAI_API_KEY"] == "api-only"
    assert "PATCHSCOPE_OPENAI_API_KEY" not in launches[1][1]
    assert "PATCHSCOPE_GITHUB_TOKEN" not in launches[1][1]
    assert launches[1][1]["PATCHSCOPE_API_URL"] == "http://127.0.0.2:9000"
    assert ui.terminated is True
    assert ui.wait_calls == [5.0]
    assert os.environ["PATCHSCOPE_OPENAI_API_KEY"] == "api-only"


def test_start_stops_both_children_on_keyboard_interrupt(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeChildProcess()
    ui = FakeChildProcess()
    children = iter([api, ui])

    monkeypatch.setattr(
        "patchscope.cli.subprocess.Popen",
        lambda _command, *, env: next(children),
    )
    monkeypatch.setattr(
        "patchscope.cli.time.sleep",
        lambda _duration: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 130
    assert api.terminated is True
    assert ui.terminated is True
    assert api.wait_calls == [5.0]
    assert ui.wait_calls == [5.0]


def test_start_rejects_a_shared_api_and_workbench_port(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "patchscope.cli.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("no child process should start"),
    )

    result = runner.invoke(app, ["start", "--api-port", "9000", "--ui-port", "9000"])

    assert result.exit_code == 2
    assert "must be different" in result.stderr


def test_start_cleans_up_the_api_when_the_workbench_cannot_launch(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeChildProcess()
    calls = 0

    def fake_popen(_command: list[str], *, env: dict[str, str]) -> FakeChildProcess:
        nonlocal calls
        del env
        calls += 1
        if calls == 2:
            raise OSError("unavailable")
        return api

    monkeypatch.setattr("patchscope.cli.subprocess.Popen", fake_popen)

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 1
    assert "could not start" in result.stdout
    assert api.terminated is True


def test_stop_child_kills_a_process_that_ignores_termination() -> None:
    child = FakeChildProcess()

    def stubborn_wait(timeout: float | None = None) -> int:
        child.wait_calls.append(timeout)
        if len(child.wait_calls) == 1:
            raise subprocess.TimeoutExpired(cmd="patchscope", timeout=timeout)
        return -9

    child.wait = stubborn_wait  # type: ignore[method-assign]

    _stop_child(child)  # type: ignore[arg-type]

    assert child.terminated is True
    assert child.killed is True
    assert child.wait_calls == [5.0, 5.0]


def test_review_reads_bounded_file_renders_finding_and_closes_service(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "unsafe.py"
    source.write_text("eval(value)\n", encoding="utf-8")
    service = RecordingService()
    monkeypatch.setattr(
        "patchscope.cli.Settings",
        lambda: SimpleNamespace(max_review_bytes=source.stat().st_size),
    )
    monkeypatch.setattr("patchscope.container.build_service", lambda _settings: service)

    result = runner.invoke(app, ["review", str(source), "--name", "Security review"])

    assert result.exit_code == 0
    assert "Risk score: 20 / 100" in result.stdout
    assert "Unsafe evaluation" in result.stdout
    assert service.uploads == [
        {
            "filename": "unsafe.py",
            "content": b"eval(value)\n",
            "title": "Security review",
        }
    ]
    assert service.closed is True


def test_review_rejects_oversized_file_before_building_service(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "too-large.py"
    source.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(
        "patchscope.cli.Settings",
        lambda: SimpleNamespace(max_review_bytes=1),
    )

    result = runner.invoke(app, ["review", str(source)])

    assert result.exit_code == 2
    assert "1-byte review limit" in result.stderr


def test_demo_json_uses_packaged_source_and_always_closes_service(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = RecordingService()
    captured_settings: list[Settings] = []

    def build(settings: Settings) -> RecordingService:
        captured_settings.append(settings)
        return service

    monkeypatch.setenv("PATCHSCOPE_AI_MODE", "openai")
    monkeypatch.setenv("PATCHSCOPE_OPENAI_API_KEY", "ambient-provider-key")
    monkeypatch.setattr("patchscope.container.build_service", build)

    result = runner.invoke(app, ["demo", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["id"] == "rev_cli"
    assert service.uploads[0]["filename"] == "insecure_checkout.py"
    assert b"eval(" in service.uploads[0]["content"]
    assert service.closed is True
    assert len(captured_settings) == 1
    assert captured_settings[0].ai_mode == "offline"


def test_analyzers_json_and_table_modes_close_their_services(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services: list[RecordingService] = []

    def build(_settings: object) -> RecordingService:
        service = RecordingService()
        services.append(service)
        return service

    monkeypatch.setattr("patchscope.container.build_service", build)

    table_result = runner.invoke(app, ["analyzers"])
    json_result = runner.invoke(app, ["analyzers", "--json"])

    assert table_result.exit_code == 0
    assert "PatchScope analyzer availability" in table_result.stdout
    assert json.loads(json_result.stdout)["analyzers"][1]["status"] == "unavailable"
    assert len(services) == 2
    assert all(service.closed for service in services)
