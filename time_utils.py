"""Local-time parsing and random scheduling helpers."""

import datetime
import random
from typing import List

from core.plugin import logger


def get_local_now() -> datetime.datetime:
    return datetime.datetime.now()


def parse_time_string(time_str: str) -> datetime.datetime:
    return datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M")


def generate_multiple_random_times(
    start_time: datetime.datetime, end_time: datetime.datetime, count: int
) -> List[datetime.datetime]:
    if count <= 0:
        return []
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


def determine_random_count(
    random_count=None, random_count_min=None, random_count_max=None
) -> int:
    if random_count and random_count > 0:
        return random_count
    if random_count_min is not None and random_count_max is not None:
        if random_count_min > random_count_max:
            random_count_min, random_count_max = random_count_max, random_count_min
        return random.randint(random_count_min, random_count_max)
    return 1
