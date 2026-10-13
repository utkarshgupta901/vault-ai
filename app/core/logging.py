"""Structured logging (structlog). JSON in prod (one event per line, easy to ship to any log store),
readable console output locally. Context such as request_id / task_id / document_id is bound via contextvars."""

import logging
import sys

import structlog

from app.config import settings


def configure_logging() -> None:
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    renderer = (
        structlog.processors.JSONRenderer()
        if settings.LOG_JSON or settings.APP_ENV == "prod"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)
    for noisy in ("httpx", "httpcore", "botocore", "boto3", "urllib3", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
