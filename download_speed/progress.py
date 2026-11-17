"""Отображение прогресса загрузок."""

from __future__ import annotations

import os
from typing import TextIO

from download_speed.models import MIB, DownloadResult

PROGRESS_UPDATE_INTERVAL_SECONDS = 0.25
DEFAULT_TERMINAL_COLUMNS = 80
TERMINAL_LAST_COLUMN_RESERVE = 1
MINIMUM_PROGRESS_BAR_WIDTH = 3
ERASE_TERMINAL_LINE = "\x1b[2K"


class ProgressRenderer:
    """Выводит компактный прогресс последовательных запросов."""

    def __init__(self, stream: TextIO) -> None:
        """Сохраняет поток вывода."""
        self.stream = stream
        self.terminal = stream.isatty()
        self.current = 0
        self.total = 0
        self.last_update_at: float | None = None
        self.active = False

    def _terminal_line_width(self) -> int:
        """Возвращает ширину строки с запасом от автопереноса."""
        try:
            columns = os.get_terminal_size(self.stream.fileno()).columns
        except (OSError, ValueError):
            columns = DEFAULT_TERMINAL_COLUMNS
        return max(0, columns - TERMINAL_LAST_COLUMN_RESERVE)

    def _write_terminal_line(
        self,
        line: str,
        *,
        finish: bool = False,
        width: int | None = None,
    ) -> None:
        """Перерисовывает одну строку в пределах полезной ширины."""
        usable_width = self._terminal_line_width() if width is None else width
        visible_line = line if len(line) <= usable_width else ""
        ending = "\n" if finish else ""
        self.stream.write(f"\r{ERASE_TERMINAL_LINE}{visible_line}{ending}")
        self.stream.flush()

    def _request_field(self, width: int) -> str:
        """Выбирает целое обозначение запроса для доступной ширины."""
        request_number = f"{self.current}/{self.total}"
        labeled_request = f"Запрос {request_number}"
        if len(labeled_request) <= width:
            return labeled_request
        if len(request_number) <= width:
            return request_number
        return ""

    def _fit_fields(self, width: int, *fields: str) -> str:
        """Добавляет только целые поля, которые помещаются в строку."""
        line = ""
        for field in fields:
            if not field:
                continue
            candidate = field if not line else f"{line} {field}"
            if len(candidate) <= width:
                line = candidate
        return line

    def _speed(self, received: int, elapsed: float) -> str:
        """Форматирует среднюю скорость с единицами измерения."""
        if elapsed <= 0:
            return "—"
        return f"{received / elapsed / MIB:.3f} МиБ/с"

    def _update_line(self, received: int, expected: int | None, elapsed: float) -> tuple[str, int]:
        """Собирает компактное обновление и подбирает ширину полосы."""
        width = self._terminal_line_width()
        request = self._request_field(width)
        speed = f"ср. {self._speed(received, elapsed)}"
        if expected is None or expected <= 0:
            return self._fit_fields(width, request, f"{received} Б", speed), width

        ratio = received / expected
        amount = f"{received}/{expected} Б"
        percentage = f"{ratio * 100:.1f}%"
        fields = (request, amount, percentage, speed)
        line_without_bar = " ".join(fields)
        if len(line_without_bar) > width:
            return self._fit_fields(width, *fields), width

        bar_width = width - len(line_without_bar) - len(" []")
        if bar_width < MINIMUM_PROGRESS_BAR_WIDTH:
            return line_without_bar, width
        bounded_ratio = min(max(ratio, 0.0), 1.0)
        filled = round(bounded_ratio * bar_width)
        bar = "#" * filled + "-" * (bar_width - filled)
        return f"{request} {amount} {percentage} [{bar}] {speed}", width

    def start(self, current: int, total: int) -> None:
        """Показывает начало и сбрасывает ограничитель частоты."""
        self.current = current
        self.total = total
        self.last_update_at = None
        self.active = True
        if self.terminal:
            width = self._terminal_line_width()
            line = self._fit_fields(width, self._request_field(width), "начало")
            self._write_terminal_line(line, width=width)
        else:
            line = f"Запрос {current}/{total}: начало"
            self.stream.write(f"{line}\n")
            self.stream.flush()

    def update(self, received: int, expected: int | None, started_at: float, now: float) -> None:
        """Показывает ограниченное по частоте терминальное обновление."""
        if not self.terminal:
            return
        previous = started_at if self.last_update_at is None else self.last_update_at
        if now - previous < PROGRESS_UPDATE_INTERVAL_SECONDS:
            return
        self.last_update_at = now

        line, width = self._update_line(received, expected, now - started_at)
        self._write_terminal_line(line, width=width)

    def finish(self, result: DownloadResult) -> None:
        """Показывает фактический объём успешного ответа."""
        if self.terminal:
            width = self._terminal_line_width()
            speed = self._speed(result.byte_count, result.duration)
            line = self._fit_fields(
                width,
                self._request_field(width),
                "завершён:",
                f"{result.byte_count} Б;",
                f"ср. {speed}",
            )
            self._write_terminal_line(line, finish=True, width=width)
        else:
            speed = self._speed(result.byte_count, result.duration)
            line = (
                f"Запрос {self.current}/{self.total}: завершён, {result.byte_count} байт; "
                f"средняя скорость: {speed}"
            )
            self.stream.write(f"{line}\n")
            self.stream.flush()
        self.active = False

    def abort(self) -> None:
        """Завершает активную терминальную строку перед сообщением об ошибке."""
        if self.terminal and self.active:
            self._write_terminal_line("", finish=True)
        self.active = False
