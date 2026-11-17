"""Проверки терминального и построчного вывода прогресса."""

from __future__ import annotations

import io
import os
from typing import Never
from urllib.request import Request

import pytest

from download_speed.cli import main
from download_speed.models import DownloadResult
from download_speed.progress import ProgressRenderer


class Output(io.StringIO):
    """Имитирует поток с выбранным терминальным режимом."""

    def __init__(self, terminal: bool) -> None:
        """Сохраняет признак терминала и отдельные записи."""
        super().__init__()
        self.terminal = terminal
        self.writes: list[str] = []

    def isatty(self) -> bool:
        """Возвращает заданный режим потока."""
        return self.terminal

    def write(self, text: str) -> int:
        """Запоминает границы обновлений строки."""
        self.writes.append(text)
        return super().write(text)


class TerminalOutput(Output):
    """Моделирует строки терминала и автоматический перенос."""

    def __init__(self, columns: int) -> None:
        """Создаёт терминал заданной ширины."""
        super().__init__(True)
        self.columns = columns
        self.rows: list[list[str]] = [[]]
        self.row = 0
        self.column = 0
        self.automatic_wraps = 0

    def fileno(self) -> int:
        """Возвращает условный дескриптор для запроса размера."""
        return 42

    def write(self, text: str) -> int:
        """Применяет возвраты каретки, очистку и переносы к экрану."""
        written = super().write(text)
        position = 0
        while position < len(text):
            if text.startswith("\x1b[2K", position):
                self.rows[self.row] = []
                position += 4
                continue
            character = text[position]
            position += 1
            if character == "\r":
                self.column = 0
                continue
            if character == "\n":
                self.row += 1
                self.column = 0
                self.rows.append([])
                continue
            if self.column == self.columns:
                self.automatic_wraps += 1
                self.row += 1
                self.column = 0
                self.rows.append([])
            row = self.rows[self.row]
            if self.column == len(row):
                row.append(character)
            else:
                row[self.column] = character
            self.column += 1
        return written

    def visible_rows(self) -> list[str]:
        """Возвращает видимое содержимое строк без правых пробелов."""
        return ["".join(row).rstrip() for row in self.rows]


class InterruptingOutput(Output):
    """Прерывает первую запись после сохранения видимого текста."""

    def __init__(self) -> None:
        """Создаёт терминальный поток с одним будущим прерыванием."""
        super().__init__(True)
        self.interrupted = False

    def write(self, text: str) -> int:
        """Прерывает только первую запись строки прогресса."""
        written = super().write(text)
        if not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt
        return written


class UnusedOpener:
    """Не допускает сеть при прерывании начального вывода."""

    def open(self, request: Request, *, timeout: float) -> Never:
        """Сообщает о преждевременном сетевом обращении."""
        raise AssertionError("network started")


def test_non_terminal_prints_only_start_and_finish_lines() -> None:
    """Вне терминала нет перерисовок и управляющих символов."""
    output = Output(False)
    progress = ProgressRenderer(output)

    progress.start(2, 10)
    progress.update(512, 1024, 1.0, 2.0)
    progress.finish(DownloadResult(1024, 2.0))

    assert output.getvalue().count("\n") == 2
    assert "\r" not in output.getvalue()
    assert "2/10" in output.getvalue()


def test_terminal_known_length_shows_bar_percent_and_average_speed() -> None:
    """Известная положительная длина добавляет полосу и процент."""
    output = Output(True)
    progress = ProgressRenderer(output)

    progress.start(1, 10)
    progress.update(512 * 1024, 1024 * 1024, 1.0, 2.0)

    line = output.writes[-1].replace("\x1b[2K", "")
    assert "524288" in line
    assert "50.0%" in line
    assert "[" in line and "]" in line
    assert "ср. 0.500 МиБ/с" in line


def test_terminal_progress_stays_on_one_row_at_eighty_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обновления, завершение и ошибка не переносятся на соседнюю строку."""
    output = TerminalOutput(80)
    monkeypatch.setattr(
        os,
        "get_terminal_size",
        lambda file_descriptor: os.terminal_size((output.columns, 24)),
    )
    progress = ProgressRenderer(output)

    progress.start(10, 10)
    progress.update(5 * 1024 * 1024, 10 * 1024 * 1024, 0.0, 1.0)
    progress.update(10 * 1024 * 1024, 10 * 1024 * 1024, 0.0, 1.25)
    progress.finish(DownloadResult(10 * 1024 * 1024, 1.25))
    progress.start(10, 10)
    progress.update(10 * 1024 * 1024, 10 * 1024 * 1024, 2.0, 3.0)
    progress.abort()

    assert output.automatic_wraps == 0
    assert all(len(row) < 80 for row in output.visible_rows())
    assert "завершён" in output.visible_rows()[0]
    assert output.visible_rows()[1] == ""


def test_terminal_progress_uses_new_width_after_resize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """После сужения терминала следующая строка очищается и умещается."""
    output = TerminalOutput(80)
    requested_descriptors: list[int] = []

    def terminal_size(file_descriptor: int) -> os.terminal_size:
        """Возвращает текущую ширину и запоминает поток запроса."""
        requested_descriptors.append(file_descriptor)
        return os.terminal_size((output.columns, 24))

    monkeypatch.setattr(os, "get_terminal_size", terminal_size)
    progress = ProgressRenderer(output)
    progress.start(1, 10)
    progress.update(5 * 1024 * 1024, 10 * 1024 * 1024, 0.0, 1.0)

    output.columns = 60
    progress.update(10 * 1024 * 1024, 10 * 1024 * 1024, 0.0, 1.25)
    progress.finish(DownloadResult(10 * 1024 * 1024, 1.25))

    assert output.automatic_wraps == 0
    assert max(map(len, output.visible_rows())) < 60
    assert requested_descriptors == [42, 42, 42, 42]


def test_very_narrow_terminal_preserves_atomic_request_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Очень узкий терминал не искажает номер запроса при сокращении полей."""
    output = TerminalOutput(12)
    monkeypatch.setattr(
        os,
        "get_terminal_size",
        lambda file_descriptor: os.terminal_size((output.columns, 24)),
    )
    progress = ProgressRenderer(output)

    progress.start(10, 10)
    start = output.writes[-1]
    progress.update(1024, 2048, 0.0, 1.0)
    known_length = output.writes[-1]
    progress.update(2048, None, 0.0, 1.25)
    unknown_length = output.writes[-1]
    progress.finish(DownloadResult(2048, 1.25))
    finish = output.writes[-1]

    visible_lines = [
        write.removeprefix("\r\x1b[2K").removesuffix("\n")
        for write in (start, known_length, unknown_length, finish)
    ]

    assert output.automatic_wraps == 0
    assert all(len(row) < 12 for row in output.visible_rows())
    assert visible_lines == ["10/10", "10/10 50.0%", "10/10", "10/10"]


def test_terminal_unknown_length_has_no_invented_percent() -> None:
    """Неизвестная длина показывает объём и скорость без процента."""
    output = Output(True)
    progress = ProgressRenderer(output)

    progress.start(1, 10)
    progress.update(1024, None, 0.0, 1.0)

    line = output.writes[-1]
    assert "1024 Б" in line
    assert "ср." in line
    assert "%" not in line
    assert "[" not in line.replace("\x1b[2K", "")


def test_terminal_updates_are_limited_to_four_per_second_and_reset() -> None:
    """Обновления идут не чаще 0,25 секунды и сбрасываются между запросами."""
    output = Output(True)
    progress = ProgressRenderer(output)
    progress.start(1, 10)

    for now in (0.10, 0.25, 0.49, 0.50):
        progress.update(100, None, 0.0, now)

    assert sum("100 Б" in write and "завершён" not in write for write in output.writes) == 2
    progress.finish(DownloadResult(100, 0.5))
    progress.start(2, 10)
    progress.update(100, None, 5.0, 5.1)
    assert sum("100 Б" in write and "завершён" not in write for write in output.writes) == 2


def test_terminal_finish_clears_longer_previous_line() -> None:
    """Завершающее сообщение затирает хвост длинной строки."""
    output = Output(True)
    progress = ProgressRenderer(output)
    progress.start(1, 10)
    progress.update(999_999, 1_000_000, 0.0, 1.0)

    progress.finish(DownloadResult(0, 0.0))

    finish = output.writes[-1]
    assert finish.endswith("\n")
    assert "0 Б" in finish
    assert "ср. —" in finish
    assert "\x1b[2K" in finish


def test_terminal_abort_finishes_active_line() -> None:
    """Ошибка после начала оставляет следующий текст на новой строке."""
    output = Output(True)
    progress = ProgressRenderer(output)
    progress.start(1, 10)

    progress.abort()

    assert output.getvalue().endswith("\n")


def test_interrupt_during_terminal_start_finishes_line() -> None:
    """Ctrl+C во время начального вывода переносит сообщение на новую строку."""
    stdout = io.StringIO()
    stderr = InterruptingOutput()

    exit_code = main(
        ["https://example.test/file"],
        stdout=stdout,
        stderr=stderr,
        opener=UnusedOpener(),
    )

    assert exit_code == 130
    assert stdout.getvalue() == ""
    assert "\nЗагрузка прервана пользователем\n" in stderr.getvalue()
