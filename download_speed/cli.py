"""Разбор параметров и консольное завершение измерения."""

from __future__ import annotations

import argparse
import math
import sys
import time
from string import punctuation
from typing import TextIO
from urllib.parse import quote, urlsplit

from download_speed.http import Opener, make_opener
from download_speed.measurement import REQUEST_COUNT, RequestFailure, run_downloads
from download_speed.models import Clock, RunSummary
from download_speed.progress import ProgressRenderer

DEFAULT_TIMEOUT = 30.0
LEADING_URL_CONTROL_OR_SPACE = "".join(chr(codepoint) for codepoint in range(0x21))


def _normalize_url_text(value: str) -> str:
    """Повторяет предварительную очистку urlsplit без реконструкции URL."""
    normalized = value.lstrip(LEADING_URL_CONTROL_OR_SPACE)
    return normalized.replace("\t", "").replace("\r", "").replace("\n", "")


def print_summary(summary: RunSummary, stream: TextIO) -> None:
    """Печатает итоговые показатели с явными единицами."""
    speed = "—" if summary.speed_mib_s is None else f"{summary.speed_mib_s:.3f} МиБ/с"
    stream.write(
        "Итог:\n"
        f"Запросов: {summary.request_count}\n"
        f"Среднее время: {summary.average_duration:.3f} с\n"
        f"Общий объём: {summary.total_bytes} байт\n"
        f"Скорость: {speed}\n"
    )


def parse_url(value: str) -> str:
    """Проверяет HTTP(S)-адрес до сетевого обращения."""
    normalized_value = _normalize_url_text(value)
    try:
        parsed = urlsplit(normalized_value)
        port = parsed.port
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"некорректный URL: {error}") from error
    if parsed.scheme.lower() not in {"http", "https"}:
        raise argparse.ArgumentTypeError("URL должен использовать схему HTTP или HTTPS")
    if parsed.hostname is None:
        raise argparse.ArgumentTypeError("URL должен содержать имя сервера")
    if port == 0:
        raise argparse.ArgumentTypeError("порт URL должен быть положительным")
    try:
        parsed.hostname.encode("ascii")
    except UnicodeEncodeError as error:
        raise argparse.ArgumentTypeError("имя сервера должно содержать только ASCII") from error
    try:
        return quote(normalized_value, encoding="utf-8", safe=punctuation)
    except UnicodeEncodeError as error:
        raise argparse.ArgumentTypeError("URL содержит недопустимые символы") from error


def parse_timeout(value: str) -> float:
    """Проверяет положительный конечный тайм-аут."""
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("тайм-аут должен быть числом") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("тайм-аут должен быть положительным конечным числом")
    return timeout


def make_parser() -> argparse.ArgumentParser:
    """Создаёт разборщик параметров командной строки."""
    parser = argparse.ArgumentParser(description="Измерение скорости десяти загрузок файла")
    parser.add_argument("URL", type=parse_url, help="HTTP(S)-адрес файла")
    parser.add_argument(
        "--timeout",
        type=parse_timeout,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=(f"тайм-аут одной блокирующей сетевой операции (по умолчанию: {DEFAULT_TIMEOUT:g})"),
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    opener: Opener | None = None,
    clock: Clock = time.perf_counter,
) -> int:
    """Запускает измерение и возвращает консольный код завершения."""
    arguments = make_parser().parse_args(argv)
    output = stdout if stdout is not None else sys.stdout
    errors = stderr if stderr is not None else sys.stderr
    active_opener = opener if opener is not None else make_opener()
    progress = ProgressRenderer(errors)
    try:
        summary = run_downloads(
            arguments.URL,
            arguments.timeout,
            progress,
            opener=active_opener,
            clock=clock,
        )
    except RequestFailure as error:
        errors.write(f"Ошибка запроса {error.request_number}/{REQUEST_COUNT}: {error}\n")
        return 1
    except KeyboardInterrupt:
        errors.write("Загрузка прервана пользователем\n")
        return 130

    print_summary(summary, output)
    return 0
