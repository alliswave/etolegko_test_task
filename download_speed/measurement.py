"""Последовательное выполнение и суммирование загрузок."""

from __future__ import annotations

import time
from typing import Protocol

from download_speed.http import DownloadError, Opener, Progress, download_once
from download_speed.models import Clock, DownloadResult, RunSummary

REQUEST_COUNT = 10


class RunProgress(Progress, Protocol):
    """Показывает жизненный цикл последовательных запросов."""

    def start(self, current: int, total: int) -> None:
        """Показывает начало запроса."""

    def finish(self, result: DownloadResult) -> None:
        """Показывает успешное завершение запроса."""

    def abort(self) -> None:
        """Завершает активную строку после сбоя."""


class RequestFailure(Exception):
    """Добавляет к ошибке номер неуспешного запроса."""

    def __init__(self, request_number: int, reason: str) -> None:
        """Сохраняет номер запроса и исходную причину."""
        super().__init__(reason)
        self.request_number = request_number


def run_downloads(
    url: str,
    timeout: float,
    progress: RunProgress,
    *,
    opener: Opener,
    clock: Clock = time.perf_counter,
) -> RunSummary:
    """Последовательно выполняет ровно десять полных загрузок."""
    total_bytes = 0
    total_duration = 0.0
    for request_number in range(1, REQUEST_COUNT + 1):
        try:
            progress.start(request_number, REQUEST_COUNT)
            result = download_once(url, timeout, progress, clock=clock, opener=opener)
            progress.finish(result)
        except DownloadError as error:
            progress.abort()
            raise RequestFailure(request_number, str(error)) from error
        except KeyboardInterrupt:
            progress.abort()
            raise
        total_bytes += result.byte_count
        total_duration += result.duration

    return RunSummary(REQUEST_COUNT, total_bytes, total_duration)
