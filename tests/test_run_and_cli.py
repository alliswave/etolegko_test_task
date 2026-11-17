"""Проверки последовательного запуска, расчётов и ошибок интерфейса."""

from __future__ import annotations

import http.client
import io
from collections.abc import Iterator, Sequence
from urllib.error import URLError
from urllib.request import Request

import pytest

import download_speed.cli as cli
import download_speed.http as download_http
import download_speed.measurement as measurement
from download_speed.models import DownloadResult, RunSummary


class FakeResponse:
    """Имитирует успешный ответ и отмечает его закрытие."""

    status = 200
    reason = "OK"

    def __init__(self, body: bytes, error: Exception | None = None) -> None:
        """Сохраняет тело и необязательную ошибку чтения."""
        self.headers = {"Content-Length": str(len(body))}
        self._chunks: Iterator[bytes] = iter((body, b""))
        self._error = error
        self.closed = False

    def read(self, size: int) -> bytes:
        """Возвращает очередную порцию ответа."""
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        return next(self._chunks)

    def __enter__(self) -> FakeResponse:
        """Возвращает открытый ответ."""
        return self

    def __exit__(self, *args: object) -> None:
        """Закрывает ответ при любом исходе."""
        self.close()

    def close(self) -> None:
        """Отмечает закрытие ответа."""
        self.closed = True


class FakeOpener:
    """Возвращает исходы строго по порядку."""

    def __init__(self, outcomes: Sequence[FakeResponse | BaseException]) -> None:
        """Сохраняет будущие ответы и ошибки."""
        self.outcomes = outcomes
        self.calls = 0

    def open(self, request: Request, *, timeout: float) -> FakeResponse:
        """Проверяет закрытие предыдущего ответа до нового запроса."""
        previous = self.outcomes[self.calls - 1] if self.calls else None
        if isinstance(previous, FakeResponse):
            assert previous.closed
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RecordingProgress:
    """Запоминает события жизненного цикла и обновления прогресса."""

    def __init__(self) -> None:
        """Создаёт пустой журнал событий."""
        self.events: list[str] = []

    def start(self, current: int, total: int) -> None:
        """Отмечает начало запроса."""
        self.events.append("start")

    def update(self, received: int, expected: int | None, started_at: float, now: float) -> None:
        """Отмечает получение данных."""
        self.events.append("update")

    def finish(self, result: DownloadResult) -> None:
        """Отмечает успешное завершение."""
        self.events.append("finish")

    def abort(self) -> None:
        """Отмечает освобождение строки после сбоя."""
        self.events.append("abort")


class ValuesClock:
    """Возвращает заданные значения монотонного времени."""

    def __init__(self, durations: list[float]) -> None:
        """Готовит начало, середину и конец каждого запроса."""
        values: list[float] = []
        current = 0.0
        for duration in durations:
            values.extend((current, current + duration / 2, current + duration))
            current += duration
        self.values = iter(values)

    def __call__(self) -> float:
        """Возвращает следующее значение."""
        return next(self.values)


class OutputCostProgress(RecordingProgress):
    """Сдвигает управляемые часы на стоимости операций вывода."""

    def __init__(self, clock_state: list[float]) -> None:
        """Связывает вывод с изменяемым значением времени."""
        super().__init__()
        self.clock_state = clock_state

    def start(self, current: int, total: int) -> None:
        """Добавляет стоимость начального вывода."""
        self.clock_state[0] += 5.0

    def update(self, received: int, expected: int | None, started_at: float, now: float) -> None:
        """Добавляет стоимость промежуточного вывода."""
        self.clock_state[0] += 2.0

    def finish(self, result: DownloadResult) -> None:
        """Добавляет стоимость завершающего вывода."""
        self.clock_state[0] += 7.0


def test_ten_downloads_are_sequential_and_use_ratio_of_totals() -> None:
    """Итог строится по суммарным байтам и длительностям десяти ответов."""
    byte_counts = [1, 9] * 5
    durations = [1.0, 3.0] * 5
    responses = [FakeResponse(b"x" * size) for size in byte_counts]
    opener = FakeOpener(responses)

    summary = measurement.run_downloads(
        "https://example.test/file",
        7.5,
        RecordingProgress(),
        opener=opener,
        clock=ValuesClock(durations),
    )

    assert opener.calls == 10
    assert all(response.closed for response in responses)
    assert summary == RunSummary(10, 50, 20.0)
    assert summary.average_duration == 2.0
    assert summary.speed_mib_s == 2.5 / (1024 * 1024)


def test_only_intermediate_output_cost_is_measured() -> None:
    """Начальный и завершающий вывод исключены, промежуточный включён."""
    clock_state = [0.0]
    progress = OutputCostProgress(clock_state)

    summary = measurement.run_downloads(
        "https://example.test/file",
        1.0,
        progress,
        opener=FakeOpener([FakeResponse(b"x") for _ in range(10)]),
        clock=lambda: clock_state[0],
    )

    assert summary.total_duration == 20.0


def test_first_error_stops_following_requests_and_closes_progress() -> None:
    """Сбой четвёртого запроса останавливает серию и завершает строку."""
    responses = [FakeResponse(b"ok") for _ in range(3)]
    opener = FakeOpener([*responses, URLError("сеть недоступна"), FakeResponse(b"late")])
    progress = RecordingProgress()

    with pytest.raises(measurement.RequestFailure) as captured:
        measurement.run_downloads(
            "https://example.test/file",
            1.0,
            progress,
            opener=opener,
            clock=ValuesClock([1.0, 1.0, 1.0, 1.0]),
        )

    assert captured.value.request_number == 4
    assert "сеть недоступна" in str(captured.value)
    assert opener.calls == 4
    assert progress.events[-1] == "abort"


def test_read_error_closes_response() -> None:
    """Ошибка чтения не оставляет окончательный ответ открытым."""
    response = FakeResponse(b"", OSError("соединение сброшено"))

    with pytest.raises(download_http.DownloadError, match="соединение сброшено"):
        download_http.download_once(
            "https://example.test/file",
            1.0,
            RecordingProgress(),
            opener=FakeOpener([response]),
            clock=lambda: 0.0,
        )

    assert response.closed


def test_bad_status_line_is_reported_without_starting_next_request() -> None:
    """Неверная строка статуса даёт код 1 без сводки и следующего запроса."""
    opener = FakeOpener([http.client.BadStatusLine("broken"), FakeResponse(b"late")])
    stdout, stderr = io.StringIO(), io.StringIO()

    exit_code = cli.main(
        ["https://example.test/file"],
        stdout=stdout,
        stderr=stderr,
        opener=opener,
        clock=lambda: 0.0,
    )

    assert exit_code == 1
    assert opener.calls == 1
    assert stdout.getvalue() == ""
    assert "1/10" in stderr.getvalue()
    assert "broken" in stderr.getvalue()


def test_long_protocol_line_during_read_closes_response() -> None:
    """Слишком длинная строка протокола управляемо завершает чтение и ответ."""
    response = FakeResponse(b"", http.client.LineTooLong("строка протокола"))
    opener = FakeOpener([response, FakeResponse(b"late")])
    stdout, stderr = io.StringIO(), io.StringIO()

    exit_code = cli.main(
        ["https://example.test/file"],
        stdout=stdout,
        stderr=stderr,
        opener=opener,
        clock=lambda: 0.0,
    )

    assert exit_code == 1
    assert response.closed
    assert opener.calls == 1
    assert stdout.getvalue() == ""
    assert "1/10" in stderr.getvalue()
    assert "строка протокола" in stderr.getvalue()


def test_summary_handles_empty_body_and_zero_duration() -> None:
    """Пустое тело и нулевой интервал не приводят к делению на ноль."""
    assert RunSummary(10, 0, 5.0).speed_mib_s == 0.0
    zero_time = RunSummary(10, 100, 0.0)
    output = io.StringIO()

    cli.print_summary(zero_time, output)

    assert zero_time.speed_mib_s is None
    assert "Скорость: —" in output.getvalue()


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["ftp://example.test/file"],
        ["http:///missing-host"],
        ["http://example.test:bad/file"],
        ["http://example.test:0/file"],
        ["https://example.test/file", "--timeout", "0"],
        ["https://example.test/file", "--timeout", "inf"],
        ["https://example.test/file", "--timeout", "NaN"],
        ["https://example.test/file", "--timeout", "text"],
    ],
)
def test_invalid_arguments_exit_two_before_network(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Некорректные параметры дают код 2 без сетевого обращения."""
    monkeypatch.setattr(
        cli,
        "make_opener",
        lambda: (_ for _ in ()).throw(AssertionError("network started")),
    )

    with pytest.raises(SystemExit) as captured:
        cli.main(arguments)

    assert captured.value.code == 2
    assert capsys.readouterr().err


def test_help_exits_successfully_before_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Справка завершается успешно до создания сетевого клиента."""
    monkeypatch.setattr(
        cli,
        "make_opener",
        lambda: (_ for _ in ()).throw(AssertionError("network started")),
    )

    with pytest.raises(SystemExit) as captured:
        cli.main(["--help"])

    streams = capsys.readouterr()
    assert captured.value.code == 0
    assert "URL" in streams.out
    assert streams.err == ""


def test_help_uses_configured_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Справка получает значение тайм-аута из единственной настройки по умолчанию."""
    monkeypatch.setattr(cli, "DEFAULT_TIMEOUT", 12.5)

    help_text = cli.make_parser().format_help()

    assert "12.5" in help_text


def test_main_reports_failure_without_summary() -> None:
    """Сбой содержит номер запроса и оставляет stdout пустым."""
    stdout, stderr = io.StringIO(), io.StringIO()

    exit_code = cli.main(
        ["https://example.test/file"],
        stdout=stdout,
        stderr=stderr,
        opener=FakeOpener([URLError("нет маршрута")]),
        clock=lambda: 0.0,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert "1/10" in stderr.getvalue()
    assert "нет маршрута" in stderr.getvalue()


def test_main_handles_keyboard_interrupt() -> None:
    """Ctrl+C завершает активную строку и возвращает код 130."""
    stdout, stderr = io.StringIO(), io.StringIO()

    exit_code = cli.main(
        ["https://example.test/file"],
        stdout=stdout,
        stderr=stderr,
        opener=FakeOpener([KeyboardInterrupt()]),
        clock=lambda: 0.0,
    )

    assert exit_code == 130
    assert stdout.getvalue() == ""
    assert "прерван" in stderr.getvalue().lower()
