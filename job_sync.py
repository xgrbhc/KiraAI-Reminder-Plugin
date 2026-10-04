"""Synchronous scheduler changes and compensation for committed reminder data."""

from collections.abc import Callable
from typing import Any

from apscheduler.jobstores.base import JobLookupError

from core.plugin import logger


def reminder_job_commit(
    sid: str,
    *,
    add_job: Callable[[str, dict], None],
    remove_job: Callable[[str], None],
    get_scheduler: Callable[[], Any] | None = None,
) -> Callable[[dict, dict], None]:
    """Build an after-save callback; it must never await external work."""
    def remove(job_id: str) -> None:
        try:
            remove_job(job_id)
        except JobLookupError:
            pass

    def commit(before: dict, after: dict) -> None:
        previous = {r["job_id"]: r for r in before.get(sid, []) if r.get("job_id")}
        current = {r["job_id"]: r for r in after.get(sid, []) if r.get("job_id")}
        applied = []
        snapshots = {}
        in_flight = None

        def snapshot(job_id: str) -> None:
            scheduler = get_scheduler() if get_scheduler is not None else None
            if scheduler is not None and hasattr(scheduler, "get_job"):
                snapshots[job_id] = (scheduler, scheduler.get_job(job_id))

        try:
            for job_id, reminder in current.items():
                if previous.get(job_id) != reminder and not reminder.get("paused"):
                    snapshot(job_id)
                    in_flight = job_id
                    add_job(sid, reminder)
                    applied.append(job_id)
                    in_flight = None
            for job_id, reminder in previous.items():
                updated = current.get(job_id)
                if updated is None or (updated.get("paused") and updated != reminder):
                    snapshot(job_id)
                    in_flight = job_id
                    remove(job_id)
                    applied.append(job_id)
                    in_flight = None
        except Exception as error:
            # A scheduler can change its store before a later wakeup raises.
            if in_flight is not None and in_flight in snapshots:
                applied.append(in_flight)
            failures = []
            for job_id in reversed(applied):
                original = previous.get(job_id)
                try:
                    if job_id in snapshots:
                        scheduler, job = snapshots[job_id]
                        if scheduler.get_job(job_id) is job:
                            continue
                        if job is None:
                            remove(job_id)
                        else:
                            options = {
                                name: getattr(job, name) for name in (
                                    "id", "name", "trigger", "args", "kwargs", "executor",
                                    "misfire_grace_time", "coalesce", "max_instances", "next_run_time",
                                ) if hasattr(job, name)
                            }
                            scheduler.add_job(job.func, replace_existing=True, **options)
                    elif original is not None and not original.get("paused"):
                        add_job(sid, original)
                    else:
                        remove(job_id)
                except Exception as rollback_error:
                    failures.append(job_id)
                    logger.error(f"[Reminder] Job compensation failed id={job_id}: {rollback_error}")
            if failures:
                raise RuntimeError("调度更新失败，原任务恢复未完成，请检查日志后重载插件") from error
            raise

    return commit
