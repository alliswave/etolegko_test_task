"""Общие модели и единицы измерения."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

MIB = 1024 * 1024


class Clock(Protocol):
    """Возвращает монотонное время в секундах."""

    def __call__(self) -> float:
        """Возвращает очередное значение времени."""


@dataclass(frozen=True)
class DownloadResult:
    """Хранит фактический объём и длительность одной загрузки."""

    byte_count: int
    duration: float


@dataclass(frozen=True)
class RunSummary:
    """Хранит точные суммарные показатели успешного запуска."""

    request_count: int
    total_bytes: int
    total_duration: float

    @property
    def average_duration(self) -> float:
        """Возвращает среднюю длительность запроса."""
        return self.total_duration / self.request_count

    @property
    def speed_mib_s(self) -> float | None:
        """Возвращает общую скорость или отсутствие измеримого интервала."""
        if self.total_duration <= 0:
            return None
        return self.total_bytes / self.total_duration / MIB
