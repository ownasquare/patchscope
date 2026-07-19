"""PatchScope command-line interface and local launchers."""

from __future__ import annotations

import json
import os

# This launcher invokes only the fixed Streamlit module command assembled below.
import subprocess  # nosec B404
import sys
import time
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Annotated, cast

import typer
from rich.console import Console
from rich.table import Table

from patchscope import __version__
from patchscope.config import Settings
from patchscope.service import dump_public

app = typer.Typer(
    name="patchscope",
    help="Evidence-backed code review and safe refactor previews.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
console = Console()
SAFE_RUNTIME_ENV_KEYS = frozenset(
    {
        "COMSPEC",
        "CURL_CA_BUNDLE",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "VIRTUAL_ENV",
        "WINDIR",
    }
)
API_SETTINGS_ENV_KEYS = frozenset(
    f"PATCHSCOPE_{field_name}".upper() for field_name in Settings.model_fields
)
UI_ENV_KEYS = frozenset({"PATCHSCOPE_API_URL"})


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"PatchScope {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = None,
) -> None:
    """Review code with evidence, not vibes."""


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="API bind host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65_535, help="API port.")] = 8787,
    reload: Annotated[bool, typer.Option(help="Reload on local source changes.")] = False,
) -> None:
    """Start the FastAPI service."""

    import uvicorn

    uvicorn.run("patchscope.api.app:app", host=host, port=port, reload=reload)


def _streamlit_command(*, host: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(Path(__file__).with_name("streamlit_app.py")),
        "--server.address",
        host,
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]


def _api_command(*, host: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "patchscope.api.app:app",
        "--host",
        host,
        "--port",
        str(port),
    ]


def _client_host(bind_host: str) -> str:
    if bind_host in {"0.0.0.0", "::"}:  # noqa: S104  # nosec B104
        # Wildcard binds are an explicit CLI choice; browser links stay on loopback.
        return "127.0.0.1"
    if ":" in bind_host and not bind_host.startswith("["):
        return f"[{bind_host}]"
    return bind_host


def _selected_environment(keys: frozenset[str]) -> dict[str, str]:
    return {
        normalized: value
        for key, value in os.environ.items()
        if (normalized := key.upper()) in keys
    }


def _runtime_environment() -> dict[str, str]:
    return _selected_environment(SAFE_RUNTIME_ENV_KEYS)


def _api_environment() -> dict[str, str]:
    environment = _runtime_environment()
    environment.update(_selected_environment(API_SETTINGS_ENV_KEYS))
    return environment


def _ui_environment(*, api_url: str | None = None) -> dict[str, str]:
    environment = _runtime_environment()
    environment.update(_selected_environment(UI_ENV_KEYS))
    if api_url is not None:
        environment["PATCHSCOPE_API_URL"] = api_url
    return environment


def _stop_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


@app.command()
def start(
    host: Annotated[str, typer.Option(help="Local bind host for both services.")] = "127.0.0.1",
    api_port: Annotated[int, typer.Option(min=1, max=65_535, help="API port.")] = 8787,
    ui_port: Annotated[int, typer.Option(min=1, max=65_535, help="Workbench port.")] = 8501,
) -> None:
    """Start the API and workbench together."""

    if api_port == ui_port:
        raise typer.BadParameter("API and workbench ports must be different", param_hint="port")

    child_processes: list[subprocess.Popen[bytes]] = []
    api_environment = _api_environment()
    client_host = _client_host(host)
    ui_environment = _ui_environment(api_url=f"http://{client_host}:{api_port}")

    try:
        api_process = subprocess.Popen(  # nosec B603
            _api_command(host=host, port=api_port), env=api_environment
        )
        child_processes.append(api_process)
        ui_process = subprocess.Popen(  # nosec B603
            _streamlit_command(host=host, port=ui_port), env=ui_environment
        )
        child_processes.append(ui_process)
        console.print("[bold]PatchScope is running[/bold]")
        console.print(f"Workbench: http://{client_host}:{ui_port}")
        console.print(f"API docs:  http://{client_host}:{api_port}/docs")
        console.print("Press Ctrl+C to stop both services.")

        while True:
            for process in child_processes:
                returncode = process.poll()
                if returncode is not None:
                    raise typer.Exit(returncode)
            time.sleep(0.2)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except OSError:
        console.print("[red]PatchScope could not start a required local service.[/red]")
        raise typer.Exit(1) from None
    finally:
        for process in reversed(child_processes):
            _stop_child(process)


@app.command()
def ui(
    host: Annotated[str, typer.Option(help="Workbench bind host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65_535, help="Workbench port.")] = 8501,
) -> None:
    """Start the Streamlit review workbench."""

    command = _streamlit_command(host=host, port=port)
    completed = subprocess.run(command, check=False, env=_ui_environment())  # nosec B603
    raise typer.Exit(completed.returncode)


@app.command()
def review(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    name: Annotated[str | None, typer.Option(help="Display name for the review.")] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the full JSON contract.")
    ] = False,
) -> None:
    """Review a local source file without executing it."""

    settings = Settings()
    content = path.read_bytes()
    if len(content) > settings.max_review_bytes:
        raise typer.BadParameter(
            f"Input exceeds the {settings.max_review_bytes}-byte review limit",
            param_hint="path",
        )
    from patchscope.container import build_service

    service = build_service(settings)
    try:
        result = service.review_upload(filename=path.name, content=content, title=name)
        _render_review(result, json_output=json_output)
    finally:
        service.close()


@app.command()
def demo(
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the full JSON contract.")
    ] = False,
) -> None:
    """Run the credential-free checkout review demo."""

    example = files("patchscope.data").joinpath("insecure_checkout.py.txt")
    from patchscope.container import build_service

    service = build_service(Settings(ai_mode="offline"))
    try:
        result = service.review_upload(
            filename="insecure_checkout.py",
            content=example.read_bytes(),
            title="Insecure checkout demo",
        )
        _render_review(result, json_output=json_output)
    finally:
        service.close()


@app.command(name="analyzers")
def analyzers_command(
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Show analyzer and provider availability without reviewing source."""

    from patchscope.container import build_service

    service = build_service(Settings())
    try:
        payload = service.capabilities()
        if json_output:
            console.print_json(json.dumps(payload))
            return
        table = Table(title="PatchScope analyzer availability")
        table.add_column("Analyzer")
        table.add_column("Status")
        table.add_column("Detail")
        analyzer_items = cast(list[Mapping[str, object]], payload["analyzers"])
        for item in analyzer_items:
            table.add_row(str(item["name"]), str(item["status"]), str(item["detail"]))
        console.print(table)
    finally:
        service.close()


def _render_review(review_value: object, *, json_output: bool) -> None:
    payload = dump_public(review_value)
    if json_output:
        console.print_json(json.dumps(payload, default=str))
        return
    summary = payload["summary"]
    console.print(f"[bold]Review[/bold] {payload['id']}")
    console.print(f"Risk score: [bold]{summary['risk_score']}[/bold] / 100")
    console.print(f"Recommendation: [bold]{summary['recommendation']}[/bold]")
    table = Table(title="Findings")
    table.add_column("Severity")
    table.add_column("Category")
    table.add_column("Location")
    table.add_column("Finding")
    for finding in payload["findings"]:
        table.add_row(
            str(finding["severity"]),
            str(finding["category"]),
            f"{finding['path']}:{finding['start_line']}",
            str(finding["title"]),
        )
    console.print(table)


if __name__ == "__main__":
    app()
