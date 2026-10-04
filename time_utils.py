"""Local-time parsing and random scheduling helpers."""

import datetime
import random
from typing import List

from core.plugin import logger


MAX_RANDOM_REMINDERS = 100


def get_local_now() -> datetime.datetime:
    return datetime.datetime.now()


def parse_time_string(time_str: str) -> datetime.datetime:
    return datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M")


def generate_multiple_random_times(
    start_time: datetime.datetime, end_time: datetime.datetime, count: int
) -> List[datetime.datetime]:
    _validate_random_count(count, "random_count")
    time_diff = int((end_time - start_time).total_seconds())
    if time_diff <= 0:
        return [start_time] * count
    min_interval = 60
    max_possible = time_diff // min_interval
    actual = min(count, max(1, max_possible))
    if actual < count:
        logger.warning(f"[Reminder] 时间范围不足以容纳 {count} 个提醒，已调整为 {actual} 个")
    seg = time_diff // actual
    times = []
    for i in range(actual):
        seg_start = start_time + datetime.timedelta(seconds=i * seg)
        offset = random.randint(0, max(seg - 1, 0))  # Preserve zero-length segment handling.
        times.append(seg_start + datetime.timedelta(seconds=offset))
    times.sort()
    return times


def _validate_random_count(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} 必须为正整数，本次未创建")
    if value > MAX_RANDOM_REMINDERS:
        raise ValueError(f"单次随机提醒最多 {MAX_RANDOM_REMINDERS} 次，本次未创建，请减少次数")


def validate_random_count_params(
    random_count: int | None = None,
    random_count_min: int | None = None,
    random_count_max: int | None = None,
) -> None:
    """Validate effective inputs without drawing a random count or allocating jobs."""
    if random_count is not None:
        _validate_random_count(random_count, "random_count")
        return
    if random_count_min is None and random_count_max is None:
        return
    if random_count_min is None or random_count_max is None:
        raise ValueError("随机次数区间需同时提供 random_count_min 和 random_count_max，本次未创建")
    _validate_random_count(random_count_min, "random_count_min")
    _validate_random_count(random_count_max, "random_count_max")


def determine_random_count(
    random_count: int | None = None,
    random_count_min: int | None = None,
    random_count_max: int | None = None,
) -> int:
    validate_random_count_params(random_count, random_count_min, random_count_max)
    if random_count is not None:
        return random_count
    if random_count_min is not None and random_count_max is not None:
        if random_count_min > random_count_max:
            random_count_min, random_count_max = random_count_max, random_count_min
        return random.randint(random_count_min, random_count_max)
    return 1
