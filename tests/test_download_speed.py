"""Проверки консольного измерителя скорости загрузки."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import ClassVar

import pytest

import download_speed.cli as cli
import download_speed.http as download_http
import download_speed.measurement as measurement
from download_speed.models import DownloadResult

BODY_LARGER_THAN_ONE_READ = b"a" * (64 * 1024 + 17)
EMPTY_QUERY_BODY = b"query-ok"


class SilentProgress:
    """Игнорирует события прогресса без вывода."""

    def start(self, current: int, total: int) -> None:
        """Принимает начало запроса."""

    def update(self, received: int, expected: int | None, started_at: float, now: float) -> None:
        """Принимает событие прогресса."""

    def finish(self, result: DownloadResult) -> None:
        """Принимает успешное завершение."""

    def abort(self) -> None:
        """Принимает завершение после ошибки."""


class BodyHandler(BaseHTTPRequestHandler):
    """Отдаёт управляемое тело несколькими записями."""

    body: ClassVar[bytes] = BODY_LARGER_THAN_ONE_READ
    request_count: ClassVar[int] = 0
    active_requests: ClassVar[int] = 0
    overlapped: ClassVar[bool] = False
    seen_paths: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        """Отправляет тело по частям с известной длиной."""
        type(self).request_count += 1
        type(self).seen_paths.append(self.path)
        type(self).active_requests += 1
        type(self).overlapped |= type(self).active_requests > 1
        response_body = EMPTY_QUERY_BODY if self.path == "/file?" else self.body
        self.send_response(200)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body[:19])
        self.wfile.write(response_body[19:])
        self.wfile.flush()
        type(self).active_requests -= 1

    def log_message(self, format: str, *args: object) -> None:
        """Не засоряет вывод тестов журналом сервера."""


@contextmanager
def serve_body() -> Iterator[str]:
    """Запускает локальный сервер и гарантированно завершает его."""
    BodyHandler.request_count = 0
    BodyHandler.active_requests = 0
    BodyHandler.overlapped = False
    BodyHandler.seen_paths = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), BodyHandler)
    thread = Thread(target=server.serve_forever)
    started = False
    try:
        thread.start()
        started = True
        address = server.server_address
        host, port = str(address[0]), int(address[1])
        yield f"http://{host}:{port}/file"
    finally:
        try:
            if started:
                server.shutdown()
        finally:
            try:
                server.server_close()
            finally:
                if started:
                    thread.join()


@pytest.fixture
def body_server() -> Iterator[str]:
    """Предоставляет адрес локального сервера с тестовым телом."""
    with serve_body() as url:
        yield url


def test_download_once_reads_full_body_in_bounded_chunks(body_server: str) -> None:
    """Загрузка считает всё тело, превышающее размер одной порции."""
    times = iter((10.0, 10.5, 11.5, 12.5))

    result = download_http.download_once(
        body_server,
        1.0,
        SilentProgress(),
        clock=lambda: next(times),
    )

    assert result == DownloadResult(len(BodyHandler.body), 2.5)


def test_run_reads_ten_local_responses_strictly_sequentially(body_server: str) -> None:
    """Полный запуск читает десять локальных тел без перекрытия запросов."""
    summary = measurement.run_downloads(
        body_server,
        1.0,
        SilentProgress(),
        opener=download_http.make_opener(),
    )

    assert BodyHandler.request_count == 10
    assert not BodyHandler.overlapped
    assert summary.total_bytes == len(BodyHandler.body) * 10


def test_huge_finite_timeout_is_reported_as_request_error(body_server: str) -> None:
    """Слишком большой конечный тайм-аут даёт управляемую ошибку первого запроса."""
    stdout, stderr = io.StringIO(), io.StringIO()

    exit_code = cli.main(
        [body_server, "--timeout", "1e100"],
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert "1/10" in stderr.getvalue()
    assert BodyHandler.request_count <= 1


def test_initial_unicode_path_is_encoded_before_network(body_server: str) -> None:
    """Путь с не-ASCII символом кодируется до десяти сетевых запросов."""
    stdout, stderr = io.StringIO(), io.StringIO()
    unicode_url = body_server.replace("/file", "/café")

    exit_code = cli.main([unicode_url], stdout=stdout, stderr=stderr)

    assert exit_code == 0
    assert BodyHandler.request_count == 10
    assert BodyHandler.seen_paths == ["/caf%C3%A9"] * 10
    assert "Запросов: 10" in stdout.getvalue()


def test_initial_url_normalization_uses_parsed_representation(body_server: str) -> None:
    """Нормализация удаляет начальные controls, но кодирует пробелы пути и Unicode."""
    stdout, stderr = io.StringIO(), io.StringIO()
    meaningful_url = body_server.replace("/file", "/a b%20c?value=café%2F#ф")
    prefixed_url = f" \t\n{meaningful_url}"

    exit_code = cli.main([prefixed_url], stdout=stdout, stderr=stderr)

    assert exit_code == 0
    assert BodyHandler.request_count == 10
    assert BodyHandler.seen_paths == ["/a%20b%20c?value=caf%C3%A9%2F"] * 10
    assert "Запросов: 10" in stdout.getvalue()


@pytest.mark.parametrize("suffix", ["?", "?#fragment"])
def test_initial_url_preserves_explicit_empty_query(body_server: str, suffix: str) -> None:
    """Пустой query сохраняет разделитель, а фрагмент не отправляется серверу."""
    stdout, stderr = io.StringIO(), io.StringIO()

    exit_code = cli.main([body_server + suffix], stdout=stdout, stderr=stderr)

    assert exit_code == 0
    assert BodyHandler.seen_paths == ["/file?"] * 10
    assert "Общий объём: 80 байт" in stdout.getvalue()


def test_body_server_closes_socket_when_thread_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ошибка запуска потока закрывает сокет без остановки незапущенного сервера."""
    closed_descriptors: list[int] = []
    original_close = ThreadingHTTPServer.server_close

    def record_close(server: ThreadingHTTPServer) -> None:
        """Закрывает сервер и сохраняет итоговый дескриптор."""
        original_close(server)
        closed_descriptors.append(server.socket.fileno())

    def fail_start(thread: Thread) -> None:
        """Имитирует отказ запуска потока."""
        raise RuntimeError("поток не запущен")

    def reject_shutdown(server: ThreadingHTTPServer) -> None:
        """Запрещает остановку незапущенного сервера."""
        raise AssertionError("shutdown called before start")

    monkeypatch.setattr(ThreadingHTTPServer, "server_close", record_close)
    monkeypatch.setattr(ThreadingHTTPServer, "shutdown", reject_shutdown)
    monkeypatch.setattr(Thread, "start", fail_start)

    with pytest.raises(RuntimeError, match="поток не запущен"), serve_body():
        pass

    assert closed_descriptors == [-1]
