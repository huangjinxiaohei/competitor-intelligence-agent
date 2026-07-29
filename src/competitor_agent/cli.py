"""Typer command-line interface for the portable competitor-intelligence loop."""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from typing import Any, Callable

import typer
from dotenv import load_dotenv

app = typer.Typer(help="Discover, analyse, report, and deliver competitor intelligence.")
DEFAULT_CONFIG = Path("config/project.yaml")


def _pipeline_function(name: str) -> Callable[..., Any]:
    """Resolve late so operational diagnostics work before pipeline wiring exists."""
    from competitor_agent import pipeline

    return getattr(pipeline, name)


def _invoke_pipeline(name: str, **kwargs: Any) -> Any:
    function = _pipeline_function(name)
    signature = inspect.signature(function)
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return function(**kwargs)
    accepted = {key: value for key, value in kwargs.items() if key in signature.parameters}
    return function(**accepted)


def _emit(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, (dict, list, tuple)):
        typer.echo(json.dumps(value, ensure_ascii=False, default=str, indent=2))
    else:
        typer.echo(str(value))


def _common_options(
    config: Path, fixture: bool, dry_run: bool
) -> dict[str, Any]:
    return {"config_path": config, "fixture": fixture, "dry_run": dry_run}


@app.command()
def doctor(
    fixture: bool = typer.Option(False, "--fixture", help="Use bundled synthetic fixture data."),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", exists=False),
) -> None:
    """Check local configuration and delivery readiness without making a network call."""
    load_dotenv(dotenv_path=Path(".env"), override=False)
    typer.echo(f"Config: {'present' if config.exists() else 'not found'} ({config})")
    if fixture:
        typer.echo("Fixture mode: ready; mock delivery is available.")
    else:
        typer.echo("Fixture mode: disabled.")
    selected_delivery = os.getenv("DELIVERY_ADAPTER")
    if selected_delivery is None and config.exists():
        try:
            from competitor_agent.config import load_config

            selected_delivery = load_config(config).adapters.delivery
        except Exception:
            selected_delivery = "unknown"
    selected_delivery = selected_delivery or "mock"
    webhook_configured = bool(os.getenv("DELIVERY_WEBHOOK_URL"))
    if selected_delivery == "webhook" and webhook_configured:
        typer.echo("Webhook adapter: configured and selected.")
    elif selected_delivery == "webhook":
        typer.echo("Webhook adapter: selected but DELIVERY_WEBHOOK_URL is not configured.")
    else:
        url_state = "configured" if webhook_configured else "not configured"
        typer.echo(
            f"Webhook adapter: not selected (delivery adapter: {selected_delivery}; URL: {url_state})."
        )


@app.command()
def discover(
    fixture: bool = typer.Option(False, "--fixture"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config"),
) -> None:
    """Run discovery only."""
    _emit(_invoke_pipeline("discover_only", **_common_options(config, fixture, dry_run)))


@app.command()
def run(
    fixture: bool = typer.Option(False, "--fixture"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config"),
) -> None:
    """Run the complete collection, comparison, reporting, and delivery loop."""
    _emit(_invoke_pipeline("run_pipeline", **_common_options(config, fixture, dry_run)))


@app.command()
def report(
    run_id: str = typer.Argument(..., help="Existing run identifier."),
    fixture: bool = typer.Option(False, "--fixture"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config"),
) -> None:
    """Render or retrieve the report for one run."""
    _emit(
        _invoke_pipeline(
            "report_run", run_id=run_id, **_common_options(config, fixture, dry_run)
        )
    )


@app.command(name="push-test")
def push_test(
    fixture: bool = typer.Option(False, "--fixture"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config"),
) -> None:
    """Exercise the end-to-end loop with the in-memory delivery adapter."""
    _emit(
        _invoke_pipeline(
            "run_pipeline",
            delivery_adapter="mock",
            force_publish=True,
            **_common_options(config, fixture, dry_run),
        )
    )


if __name__ == "__main__":  # pragma: no cover
    app()
