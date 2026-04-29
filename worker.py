from __future__ import annotations

import argparse
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import multiprocessing
import os
import socket
import time
from typing import Any

from config import get_analysis_config
from danmaku_backend.runtime.bootstrap import ensure_directories
from danmaku_backend.runtime.logging_bus import set_job_event_writer
from danmaku_backend.services.analysis_jobs import run_analysis_job
from danmaku_backend.services.artifacts import default_store
from danmaku_backend.services.jobs import default_job_store
from danmaku_backend.settings import STATE_DIR


WORKER_LOG_FILE = os.getenv(
    "BILI_DANMAKU_WORKER_LOG_FILE",
    "/www/wwwlogs/python/bilibili_danmaku/worker.log",
)
WORKER_LOCK_FILE = os.getenv(
    "BILI_DANMAKU_WORKER_LOCK_FILE",
    str(STATE_DIR / "worker.lock"),
)
_LOCK_HANDLE = None


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("bilibili_danmaku_worker")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
    try:
        os.makedirs(os.path.dirname(WORKER_LOG_FILE), exist_ok=True)
        handler = RotatingFileHandler(WORKER_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5)
    except Exception:
        handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def _process_context() -> multiprocessing.context.BaseContext:
    try:
        return multiprocessing.get_context("fork")
    except ValueError:
        return multiprocessing.get_context()


def acquire_singleton_lock(logger: logging.Logger):
    global _LOCK_HANDLE
    if _LOCK_HANDLE is not None:
        return _LOCK_HANDLE
    os.makedirs(os.path.dirname(WORKER_LOCK_FILE), exist_ok=True)
    handle = open(WORKER_LOCK_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("analysis worker already running; exiting duplicate process")
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    _LOCK_HANDLE = handle
    return handle


def _run_job_process(job: dict[str, Any]) -> None:
    ensure_directories()
    default_store.ensure()
    default_job_store.ensure()
    set_job_event_writer(default_job_store.add_event)
    run_analysis_job(job)


def prune_finished(workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alive_workers: list[dict[str, Any]] = []
    for worker in workers:
        process = worker["process"]
        if process.is_alive():
            alive_workers.append(worker)
            continue
        process.join(timeout=0)
    return alive_workers


def terminate_overdue_workers(
    workers: list[dict[str, Any]],
    timeout_seconds: int,
    logger: logging.Logger,
) -> None:
    now = time.monotonic()
    for worker in workers:
        runtime = now - float(worker["started_monotonic"])
        if runtime < timeout_seconds:
            continue
        process = worker["process"]
        if not process.is_alive():
            continue
        logger.warning(
            "terminating overdue job=%s kind=%s pid=%s runtime=%.1fs",
            worker["job_id"],
            worker["kind"],
            process.pid,
            runtime,
        )
        process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            logger.warning(
                "force killing overdue job=%s kind=%s pid=%s",
                worker["job_id"],
                worker["kind"],
                process.pid,
            )
            process.kill()
            process.join(timeout=5)
        default_job_store.mark_failed(
            worker["job_id"],
            "任务执行超时，已自动结束",
            lease_token=worker["lease_token"],
        )


def run_worker_loop(poll_interval: float = 1.0, once: bool = False) -> int:
    logger = configure_logging()
    ensure_directories()
    default_store.ensure()
    default_job_store.ensure()
    set_job_event_writer(default_job_store.add_event)
    if acquire_singleton_lock(logger) is None:
        return 0
    process_context = _process_context()

    owner_id = f"{socket.gethostname()}:{os.getpid()}"
    logger.info("analysis worker started owner=%s", owner_id)
    active_workers: list[dict[str, Any]] = []

    while True:
        terminate_overdue_workers(
            active_workers,
            max(60, int(get_analysis_config()["running_job_timeout_seconds"])),
            logger,
        )
        active_workers = prune_finished(active_workers)
        analysis_config = get_analysis_config()
        max_concurrent = max(1, int(analysis_config["max_concurrent_jobs"]))
        content_max_concurrent = max(
            1,
            min(max_concurrent, int(analysis_config.get("content_max_concurrent_jobs", max_concurrent))),
        )
        deep_max_concurrent = max(
            1,
            min(max_concurrent, int(analysis_config.get("deep_max_concurrent_jobs", 1))),
        )
        lease_seconds = max(60, int(analysis_config["running_job_timeout_seconds"]))
        kind_limits = {
            "content_analysis": content_max_concurrent,
            "deep_analysis": deep_max_concurrent,
        }

        try:
            orphaned = default_job_store.recover_orphaned_running(socket.gethostname())
            if orphaned:
                logger.warning("recovered orphaned jobs=%s", orphaned)

            recovered = default_job_store.recover_stale_running(lease_seconds)
            if recovered:
                logger.warning("recovered stale jobs=%s", recovered)

            claimed_any = False
            while len(active_workers) < max_concurrent:
                job = default_job_store.claim_next(
                    max_concurrent,
                    owner_id=owner_id,
                    lease_seconds=lease_seconds,
                    kind_limits=kind_limits,
                )
                if not job:
                    break
                claimed_any = True
                logger.info("claimed job=%s kind=%s", job["job_id"], job["kind"])
                process = process_context.Process(
                    target=_run_job_process,
                    args=(job,),
                    name=f"analysis-job-{job['job_id'][:8]}",
                    daemon=False,
                )
                process.start()
                active_workers.append(
                    {
                        "process": process,
                        "job_id": job["job_id"],
                        "kind": job["kind"],
                        "lease_token": job.get("lease_token"),
                        "started_monotonic": time.monotonic(),
                    }
                )

            if once and not active_workers:
                return 0 if not claimed_any else 1
        except Exception:
            logger.exception("worker loop error")

        time.sleep(poll_interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bilibili danmaku analysis worker")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true", help="claim available work once and exit when idle")
    args = parser.parse_args()
    return run_worker_loop(poll_interval=args.poll_interval, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
