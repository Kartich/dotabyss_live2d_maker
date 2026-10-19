# -*- coding: utf-8 -*-
"""Shared concurrency settings for Dot Offline Resource Maintainer."""
from __future__ import annotations
import os


def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 64) -> int:
    try:
        value = int(os.environ.get(name, default))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


IO_WORKERS = _env_int("DOT_IO_WORKERS", 30, 1, 64)
# v1.0 concurrency fix: use the requested 30-thread pool consistently for both
# I/O and batch extraction. Override DOT_CPU_WORKERS / DOT_IO_WORKERS if a machine
# needs a lower value because of memory pressure.
CPU_WORKERS = _env_int("DOT_CPU_WORKERS", 30, 1, 30)


def workers_for(task_count: int | None = None, *, cpu_bound: bool = False, requested: int | None = None) -> int:
    base = CPU_WORKERS if cpu_bound else IO_WORKERS
    if requested is not None:
        try:
            base = int(requested)
        except Exception:
            pass
    base = max(1, min(64 if not cpu_bound else 30, base))
    if task_count is not None:
        base = min(base, max(1, int(task_count)))
    return base
