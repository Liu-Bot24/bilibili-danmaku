from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from multiprocessing import Queue as MPQueue
from queue import Empty, Full
from typing import Callable, Iterator


log_queue = MPQueue(maxsize=1000)
_current_job_context: ContextVar[tuple[str, str | None] | None] = ContextVar(
    "current_job_context",
    default=None,
)
_job_event_writer: Callable[[str, str, str, str | None], None] | None = None


def set_job_event_writer(writer: Callable[[str, str, str, str | None], None] | None) -> None:
    global _job_event_writer
    _job_event_writer = writer


@contextmanager
def job_logging_context(job_id: str, lease_token: str | None = None) -> Iterator[None]:
    token = _current_job_context.set((job_id, lease_token))
    try:
        yield
    finally:
        _current_job_context.reset(token)


def _put(kind: str, message: str) -> None:
    text = str(message)
    item = {"type": kind, "message": text}
    try:
        # Realtime progress logs are best-effort; a full browser log stream must
        # never slow down the request that is doing the actual work. Keep the
        # newest progress by dropping one stale queued item when the buffer is full.
        log_queue.put_nowait(item)
    except Full:
        try:
            log_queue.get_nowait()
        except Empty:
            pass
        except Exception:
            pass
        try:
            log_queue.put_nowait(item)
        except Full:
            pass
        except Exception:
            pass
    except Exception:
        pass
    job_context = _current_job_context.get()
    if job_context and _job_event_writer:
        job_id, lease_token = job_context
        try:
            _job_event_writer(job_id, kind, text, lease_token)
        except Exception:
            pass


def log_info(message: str) -> None:
    _put("info", message)


def log_success(message: str) -> None:
    _put("success", message)


def log_error(message: str) -> None:
    _put("error", message)


def log_api(message: str) -> None:
    _put("api", message)
