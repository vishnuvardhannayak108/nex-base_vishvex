"""Structured logging (structlog) and the pipeline observability skeleton.

Every event is JSON with bound context. A run binds ``run_id`` once, so every
line it emits - from any module - can be grepped back to that run. ``stage``
times each pipeline stage and records its outcome for the run report.
"""
from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from typing import Iterator

import structlog

_configuration_state: dict[str, bool] = {}


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog with the given level and output format."""
    min_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(min_level),
        # stderr, so a command's own output on stdout stays parseable.
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(level=min_level, format="%(message)s", stream=sys.stderr)


def ensure_logging_configured(level: str = "INFO", json_output: bool = True) -> None:
    """Configure logging once per process; safe to call repeatedly."""
    if _configuration_state.get("configured"):
        return
    configure_logging(level, json_output)
    _configuration_state["configured"] = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, optionally named."""
    return structlog.get_logger(name)


def bind_run(run_id: str):
    """Tag every log line in this context with ``run_id``. Use as a ``with``."""
    return structlog.contextvars.bound_contextvars(run_id=run_id)


@contextmanager
def stage(log, name: str, metrics: dict) -> Iterator[None]:
    """Time one pipeline stage, log its outcome, and record it in ``metrics``.

    A failure is recorded and re-raised: isolation is the caller's decision.
    """
    started = time.monotonic()
    log.info("stage_start", stage=name)
    try:
        yield
    except Exception as exc:
        metrics[name] = {"status": "FAILED", "duration_ms": _ms(started),
                         "error": str(exc)}
        log.error("stage_failed", stage=name, **metrics[name])
        raise
    metrics[name] = {"status": "OK", "duration_ms": _ms(started)}
    log.info("stage_complete", stage=name, duration_ms=metrics[name]["duration_ms"])


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
