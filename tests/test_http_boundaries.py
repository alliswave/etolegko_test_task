"""Проверки границ протокола и освобождения сетевых ресурсов."""

from __future__ import annotations

import http.client
import io
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import BaseRequestHandler, TCPServer
from threading import Thread
from typing import ClassVar, Never, cast
from urllib.request import Request

import pytest

import download_speed.http as download_http
from download_speed.cli import main
from download_speed.models import DownloadResult


class SilentProgress:
    """Игнорирует события прогресса в протокольных тестах."""

    def update(self, received: int, expected: int | None, started_at: float, now: float) -> None:
        """Принимает очередное событие."""


class AdvancingClock:
    """Возвращает возрастающее время для произвольного числа порций."""

    def __init__(self) -> None:
        """Начинает отсчёт с нуля."""
        self.value = -1.0

    def __call__(self) -> float:
        """Сдвигает время на секунду."""
        self.value += 1.0
        return self.value


def fetch(url: str, timeout: float = 1.0) -> DownloadResult:
    """Загружает тестовый адрес с управляемыми часами."""
    return download_http.download_once(
        url,
        timeout,
        SilentProgress(),
        clock=AdvancingClock(),
    )


class RecordingNetworkOpener:
    """Открывает настоящие ответы и сохраняет их для проверки закрытия."""

    def __init__(self) -> None:
        """Создаёт штатный HTTP-клиент и пустой журнал."""
        self.opener = download_http.make_opener()
        self.calls = 0
        self.responses: list[download_http.Response] = []

    def open(self, request: Request, *, timeout: float) -> download_http.Response:
        """Открывает ответ штатным клиентом и запоминает ресурс."""
        self.calls += 1
        response = self.opener.open(request, timeout=timeout)
        self.responses.append(response)
        return response


@contextmanager
def serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    """Запускает локальный HTTP-сервер и гарантированно завершает его."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    started = False
    try:
        thread.start()
        started = True
        address = server.server_address
        host, port = str(address[0]), int(address[1])
        yield f"http://{host}:{port}"
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


@contextmanager
def serve_raw(response: bytes) -> Iterator[str]:
    """Отдаёт один заранее составленный HTTP-ответ."""

    class RawHandler(BaseRequestHandler):
        """Отправляет ответ без исправлений стандартного сервера."""

        def handle(self) -> None:
            """Читает заголовки запроса и отправляет подготовленные байты."""
            self.request.recv(64 * 1024)
            self.request.sendall(response)

    class ReusableServer(TCPServer):
        """Разрешает быстро переиспользовать адрес локального сервера."""

        allow_reuse_address = True

    server = ReusableServer(("127.0.0.1", 0), RawHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
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


@pytest.mark.parametrize(
    ("wire_body", "expected_body"),
    [
        (b"5\r\nhello\r\n0\r\n\r\n", b"hello"),
        (b"5\r\nhello\r\n0\r\n", b"hello"),
    ],
)
def test_chunked_body_accepts_complete_terminators(wire_body: bytes, expected_body: bytes) -> None:
    """Полный chunked-ответ допускает штатный EOF после нулевого блока."""
    response = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n" + wire_body
    )
    with serve_raw(response) as url:
        result = fetch(url)

    assert result.byte_count == len(expected_body)


@pytest.mark.parametrize(
    "wire_body",
    [
        b"5\r\nhel",
        b"5\r\nhello\r\n",
    ],
)
def test_chunked_body_rejects_detectable_truncation(wire_body: bytes) -> None:
    """Усечённый блок или отсутствующий нулевой блок дают ошибку."""
    response = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n" + wire_body
    )
    with serve_raw(response) as url, pytest.raises(download_http.DownloadError, match="оборвался"):
        fetch(url)


@pytest.mark.parametrize(
    ("transfer_encoding", "wire_body"),
    [
        ("chunked ", b"5\r\nhello\r\n0\r\n\r\n"),
        ("gzip, chunked", b"5\r\nhello\r\n0\r\n\r\n"),
        ("chunked ", b"5\r\nhello\r\n"),
    ],
)
def test_unsupported_transfer_framing_is_rejected(transfer_encoding: str, wire_body: bytes) -> None:
    """Формат передачи, который http.client не декодирует, не считается телом."""
    response = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: "
        + transfer_encoding.encode("ascii")
        + b"\r\nConnection: close\r\n\r\n"
        + wire_body
    )

    with (
        serve_raw(response) as url,
        pytest.raises(download_http.DownloadError, match="неподдерживаемый Transfer-Encoding"),
    ):
        fetch(url)


@pytest.mark.parametrize("chunk_size", ["-1", "-2"])
def test_negative_chunk_size_never_reaches_unbounded_read(
    chunk_size: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отрицательный размер блока даёт штатную ошибку без чтения до конца."""
    requested_sizes: list[int] = []
    original_safe_read = cast(
        Callable[[http.client.HTTPResponse, int], bytes],
        vars(http.client.HTTPResponse)["_safe_read"],
    )

    def record_safe_read(response: http.client.HTTPResponse, amount: int) -> bytes:
        """Запоминает размер внутреннего чтения настоящего ответа."""
        requested_sizes.append(amount)
        return original_safe_read(response, amount)

    monkeypatch.setattr(http.client.HTTPResponse, "_safe_read", record_safe_read)
    wire_body = chunk_size.encode("ascii") + b"\r\nsmall body\r\n0\r\n\r\n"
    wire_response = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n" + wire_body
    )
    opener = RecordingNetworkOpener()
    stdout, stderr = io.StringIO(), io.StringIO()

    with serve_raw(wire_response) as url:
        exit_code = main([url], stdout=stdout, stderr=stderr, opener=opener, clock=lambda: 0.0)

    assert exit_code == 1
    assert opener.calls == 1
    assert len(opener.responses) == 1
    response = opener.responses[0]
    assert isinstance(response, http.client.HTTPResponse)
    assert response.isclosed()
    assert requested_sizes == []
    assert stdout.getvalue() == ""
    assert "Ошибка запроса 1/10" in stderr.getvalue()


def test_content_length_truncation_is_rejected() -> None:
    """EOF раньше объявленной длины не считается успешной загрузкой."""
    response = b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nshort"
    with serve_raw(response) as url, pytest.raises(download_http.DownloadError, match="5 из 10"):
        fetch(url)


def test_eof_delimited_body_is_counted() -> None:
    """Ответ без известной длины читается до закрытия соединения."""
    response = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\neof-body"
    with serve_raw(response) as url:
        result = fetch(url)

    assert result.byte_count == 8


def test_chunked_takes_precedence_over_content_length() -> None:
    """При chunked конфликтующая длина не проверяется."""
    response = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 999\r\n"
        b"Connection: close\r\n\r\n3\r\nabc\r\n0\r\n\r\n"
    )
    with serve_raw(response) as url:
        result = fetch(url)

    assert result.byte_count == 3


class RoutingHandler(BaseHTTPRequestHandler):
    """Отдаёт успешные, ошибочные и перенаправляющие ответы."""

    seen_paths: ClassVar[list[str]] = []
    accept_encodings: ClassVar[list[str | None]] = []

    def do_GET(self) -> None:
        """Выбирает ответ по пути запроса."""
        self.seen_paths.append(self.path)
        self.accept_encodings.append(self.headers.get("Accept-Encoding"))
        if self.path == "/relative":
            self.send_response(302)
            self.send_header("Location", "target")
            self.end_headers()
            return
        if self.path == "/large-redirect":
            self.send_response(302)
            self.send_header("Location", "/target")
            self.send_header("Content-Length", str(100 * 1024 * 1024))
            self.send_header("Connection", "close")
            self.end_headers()
            return
        if self.path == "/unicode-redirect":
            self.send_response(302)
            self.send_header("Location", "/café")
            self.end_headers()
            return
        if self.path == "/unsafe":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
            return
        if self.path == "/malformed-host":
            self.send_response(302)
            self.send_header("Location", "http://[invalid")
            self.end_headers()
            return
        if self.path == "/malformed-port":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:bad/file")
            self.end_headers()
            return
        if self.path == "/missing":
            self.send_response(302)
            self.end_headers()
            return
        if self.path == "/loop-a":
            self.send_response(302)
            self.send_header("Location", "/loop-b")
            self.end_headers()
            return
        if self.path == "/loop-b":
            self.send_response(302)
            self.send_header("Location", "/loop-a")
            self.end_headers()
            return
        if self.path == "/empty":
            self.send_response(204)
            self.end_headers()
            return
        if self.path in {"/404", "/500"}:
            self.send_response(int(self.path[1:]))
            self.send_header("Content-Length", "1000000")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        body = b"target"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Не выводит журнал тестового сервера."""


class RepeatingRedirectHandler(BaseHTTPRequestHandler):
    """Возвращает заданное число переходов по повторяемым адресам."""

    redirect_count: ClassVar[int] = 0
    completed_redirects: ClassVar[int] = 0
    request_count: ClassVar[int] = 0
    repeat_same_url: ClassVar[bool] = False

    def do_GET(self) -> None:
        """Перенаправляет до заданного предела, затем возвращает тело."""
        type(self).request_count += 1
        if type(self).completed_redirects < type(self).redirect_count:
            type(self).completed_redirects += 1
            self.send_response(302)
            location = (
                self.path
                if type(self).repeat_same_url
                else f"/repeat-{type(self).completed_redirects % 4}"
            )
            self.send_header("Location", location)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Length", "1")
        self.end_headers()
        self.wfile.write(b"x")

    def log_message(self, format: str, *args: object) -> None:
        """Не выводит журнал тестового сервера."""


class CloseTrackingResponse(io.BytesIO):
    """Отмечает закрытие промежуточного ответа перенаправления."""


@pytest.fixture
def routing_server() -> Iterator[str]:
    """Запускает сервер маршрутов с чистой историей обращений."""
    RoutingHandler.seen_paths = []
    RoutingHandler.accept_encodings = []
    with serve(RoutingHandler) as url:
        yield url


def test_http_helpers_close_socket_when_thread_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Серверные помощники закрывают сокет при отказе запуска потока."""
    closed_descriptors: list[int] = []
    original_close = TCPServer.server_close

    def record_close(server: TCPServer) -> None:
        """Закрывает сервер и сохраняет итоговый дескриптор."""
        original_close(server)
        closed_descriptors.append(server.socket.fileno())

    def fail_start(thread: Thread) -> None:
        """Имитирует отказ запуска потока."""
        raise RuntimeError("поток не запущен")

    def reject_shutdown(server: TCPServer) -> None:
        """Запрещает остановку незапущенного сервера."""
        raise AssertionError("shutdown called before start")

    monkeypatch.setattr(TCPServer, "server_close", record_close)
    monkeypatch.setattr(TCPServer, "shutdown", reject_shutdown)
    monkeypatch.setattr(Thread, "start", fail_start)

    with pytest.raises(RuntimeError, match="поток не запущен"), serve(RoutingHandler):
        pass
    with pytest.raises(RuntimeError, match="поток не запущен"), serve_raw(b""):
        pass

    assert closed_descriptors == [-1, -1]


@pytest.mark.parametrize("path", ["/relative", "/large-redirect"])
def test_redirects_close_intermediate_body_without_draining(routing_server: str, path: str) -> None:
    """Относительный переход не пытается прочитать большое промежуточное тело."""
    result = fetch(routing_server + path)

    assert result.byte_count == len(b"target")
    assert RoutingHandler.seen_paths == [path, "/target"]
    assert RoutingHandler.accept_encodings == ["identity", "identity"]


def test_redirect_uses_header_bytes_for_non_ascii_location(routing_server: str) -> None:
    """Байты не-ASCII Location кодируются как в штатном обработчике urllib."""
    result = fetch(routing_server + "/unicode-redirect")

    assert result.byte_count == len(b"target")
    assert RoutingHandler.seen_paths == ["/unicode-redirect", "/caf%E9"]


def test_redirect_limit_counts_repeated_transitions() -> None:
    """Десять повторяемых переходов разрешены, одиннадцатый адрес не открывается."""
    RepeatingRedirectHandler.redirect_count = 10
    RepeatingRedirectHandler.completed_redirects = 0
    RepeatingRedirectHandler.request_count = 0
    RepeatingRedirectHandler.repeat_same_url = False
    with serve(RepeatingRedirectHandler) as url:
        assert fetch(url + "/repeat-0").byte_count == 1
    assert RepeatingRedirectHandler.request_count == 11

    RepeatingRedirectHandler.redirect_count = 11
    RepeatingRedirectHandler.completed_redirects = 0
    RepeatingRedirectHandler.request_count = 0
    with (
        serve(RepeatingRedirectHandler) as url,
        pytest.raises(download_http.DownloadError, match="предел перенаправлений"),
    ):
        fetch(url + "/repeat-0")
    assert RepeatingRedirectHandler.request_count == 11


def test_redirect_limit_allows_ten_transitions_to_same_url() -> None:
    """Одинаковый адрес не вводит дополнительный предел переходов."""
    RepeatingRedirectHandler.redirect_count = 10
    RepeatingRedirectHandler.completed_redirects = 0
    RepeatingRedirectHandler.request_count = 0
    RepeatingRedirectHandler.repeat_same_url = True
    with serve(RepeatingRedirectHandler) as url:
        assert fetch(url + "/same").byte_count == 1
    assert RepeatingRedirectHandler.request_count == 11

    RepeatingRedirectHandler.redirect_count = 11
    RepeatingRedirectHandler.completed_redirects = 0
    RepeatingRedirectHandler.request_count = 0
    with (
        serve(RepeatingRedirectHandler) as url,
        pytest.raises(download_http.DownloadError, match="предел перенаправлений"),
    ):
        fetch(url + "/same")
    assert RepeatingRedirectHandler.request_count == 11


@pytest.mark.parametrize("path", ["/malformed-host", "/malformed-port"])
def test_malformed_redirect_is_controlled_before_next_request(
    routing_server: str, path: str
) -> None:
    """Некорректный адрес перехода управляемо отклоняется до нового запроса."""
    with pytest.raises(download_http.DownloadError, match="адрес перенаправления"):
        fetch(routing_server + path)

    assert RoutingHandler.seen_paths == [path]


def test_malformed_redirect_closes_intermediate_response() -> None:
    """Ошибка проверки адреса закрывает промежуточный ответ."""
    response = CloseTrackingResponse()
    headers = http.client.HTTPMessage()
    headers["Location"] = "http://[invalid"
    handler = download_http.BoundedRedirectHandler()

    with pytest.raises(download_http.RedirectError, match="адрес перенаправления"):
        handler.http_error_302(
            Request("http://example.test/start"),
            response,
            302,
            "Found",
            headers,
        )

    assert response.closed


def test_no_content_is_successful_empty_body(routing_server: str) -> None:
    """Ответ 204 завершается с нулевым объёмом."""
    assert fetch(routing_server + "/empty").byte_count == 0


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/unsafe", "схему file"),
        ("/missing", "Location"),
        ("/loop-a", "предел перенаправлений"),
        ("/404", "HTTP 404"),
        ("/500", "HTTP 500"),
    ],
)
def test_http_failures_have_clear_reason(routing_server: str, path: str, message: str) -> None:
    """Ошибочные статусы и переходы сообщают конкретную причину."""
    with pytest.raises(download_http.DownloadError, match=message):
        fetch(routing_server + path)


class TimeoutOpener:
    """Имитирует тайм-аут до получения ответа."""

    def open(self, request: Request, *, timeout: float) -> Never:
        """Всегда завершает сетевую операцию по тайм-ауту."""
        raise TimeoutError("истекло время ожидания")


def test_timeout_is_translated_without_traceback() -> None:
    """Тайм-аут преобразуется в понятную ошибку загрузки."""
    with pytest.raises(download_http.DownloadError, match="истекло время ожидания"):
        download_http.download_once(
            "https://example.test/file",
            0.1,
            SilentProgress(),
            clock=AdvancingClock(),
            opener=TimeoutOpener(),
        )
