"""Получение и проверка одного HTTP-ответа."""

from __future__ import annotations

import http.client
from contextlib import closing
from http import HTTPStatus
from string import punctuation
from typing import IO, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from download_speed.models import Clock, DownloadResult

CHUNK_SIZE = 64 * 1024


class Progress(Protocol):
    """Получает события прогресса одного запроса."""

    def update(self, received: int, expected: int | None, started_at: float, now: float) -> None:
        """Показывает принятый объём."""


class ResponseHeaders(Protocol):
    """Предоставляет используемые заголовки HTTP-ответа."""

    def get(self, name: str, /) -> str | None:
        """Возвращает значение заголовка или отсутствие значения."""


class ClosableResponse(Protocol):
    """Описывает ресурс ответа, который необходимо закрыть."""

    def close(self) -> None:
        """Закрывает ответ и связанное соединение."""


class Response(ClosableResponse, Protocol):
    """Описывает используемую часть окончательного HTTP-ответа."""

    @property
    def status(self) -> int:
        """Возвращает код состояния."""

    @property
    def reason(self) -> str:
        """Возвращает описание состояния."""

    @property
    def headers(self) -> ResponseHeaders:
        """Возвращает заголовки ответа."""

    def read(self, size: int) -> bytes:
        """Читает ограниченную порцию тела."""


class _ParsedHTTPResponse(Protocol):
    """Описывает доступ к границе блока штатного HTTP-ответа."""

    chunked: bool

    def _get_chunk_left(self) -> int | None:
        """Возвращает остаток текущего или следующего блока."""

    def readinto(self, buffer: bytearray | memoryview) -> int:
        """Читает данные в буфер ограниченного размера."""


class Opener(Protocol):
    """Открывает один HTTP-запрос."""

    def open(self, request: Request, *, timeout: float) -> Response:
        """Возвращает ответ или сообщает сетевую ошибку."""


class _RedirectCountRequest(Protocol):
    """Описывает запрос со счётчиком выполненных переходов."""

    _download_redirect_count: int


class DownloadError(Exception):
    """Описывает понятную пользователю ошибку загрузки."""


class RedirectError(DownloadError):
    """Описывает недопустимое перенаправление."""


def _escape_redirect_location(location: str) -> str:
    """Кодирует исходные байты Location по правилам urllib."""
    try:
        return quote(location, encoding="iso-8859-1", safe=punctuation)
    except UnicodeEncodeError as error:
        raise RedirectError("заголовок Location содержит недопустимые символы") from error


def _validate_redirect_url(base_url: str, location: str) -> str:
    """Проверяет разрешённый адрес перенаправления до сетевого обращения."""
    try:
        new_url = urljoin(base_url, location)
        parsed = urlsplit(new_url)
        port = parsed.port
    except ValueError as error:
        raise RedirectError(f"некорректный адрес перенаправления: {error}") from error

    if parsed.scheme.lower() not in {"http", "https"}:
        scheme = parsed.scheme.lower() or "без схемы"
        raise RedirectError(f"перенаправление на схему {scheme} запрещено")
    if parsed.hostname is None:
        raise RedirectError("адрес перенаправления не содержит имя сервера")
    if port == 0:
        raise RedirectError("порт адреса перенаправления должен быть положительным")
    return new_url


class BoundedRedirectHandler(HTTPRedirectHandler):
    """Закрывает тело перенаправления без неограниченного чтения."""

    max_redirections = 10

    def http_error_302(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
    ) -> Response:
        """Переходит только на HTTP(S), не накапливая промежуточное тело."""
        try:
            location = headers.get("location") or headers.get("uri")
            if location is None:
                raise RedirectError("перенаправление не содержит заголовок Location")

            escaped_location = _escape_redirect_location(location)
            new_url = _validate_redirect_url(req.full_url, escaped_location)

            try:
                new_request = self.redirect_request(req, fp, code, msg, headers, new_url)
            except (ValueError, http.client.InvalidURL) as error:
                raise RedirectError(f"некорректный адрес перенаправления: {error}") from error
            if new_request is None:
                raise RedirectError("сервер вернул неподдерживаемое перенаправление")

            redirect_count = cast(int, getattr(req, "_download_redirect_count", 0)) + 1
            if redirect_count > self.max_redirections:
                raise RedirectError("превышен предел перенаправлений")
            cast(_RedirectCountRequest, new_request)._download_redirect_count = redirect_count
        finally:
            fp.close()

        return cast(Response, self.parent.open(new_request, timeout=req.timeout))

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def make_opener() -> Opener:
    """Создаёт стандартный HTTP-клиент с ограниченными перенаправлениями."""
    return cast(Opener, build_opener(BoundedRedirectHandler()))


def _expected_length(response: Response) -> int | None:
    """Возвращает проверяемую длину окончательного тела."""
    if response.status in {HTTPStatus.NO_CONTENT, HTTPStatus.RESET_CONTENT}:
        return 0

    transfer_encoding = response.headers.get("Transfer-Encoding")
    if transfer_encoding is not None:
        if transfer_encoding.lower() == "chunked":
            return None
        raise DownloadError(
            f"сервер использует неподдерживаемый Transfer-Encoding: {transfer_encoding}"
        )

    raw_length = response.headers.get("Content-Length")
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except ValueError as error:
        raise DownloadError("сервер указал некорректный Content-Length") from error
    if length < 0:
        raise DownloadError("сервер указал отрицательный Content-Length")
    return length


def _read_response_body(
    response: Response,
    progress: Progress,
    started_at: float,
    clock: Clock,
) -> int:
    """Читает тело порциями и проверяет доступную полноту ответа."""
    if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
        raise DownloadError(f"HTTP {response.status}: {response.reason}")

    expected = _expected_length(response)
    received = 0
    buffer = bytearray(CHUNK_SIZE)
    while True:
        if isinstance(response, http.client.HTTPResponse):
            read_count = _read_http_response_chunk(response, buffer)
        else:
            read_count = len(response.read(CHUNK_SIZE))
        if not read_count:
            break
        received += read_count
        progress.update(received, expected, started_at, clock())

    if expected is not None and received != expected:
        raise DownloadError(f"ответ оборвался: получено {received} из {expected} байт")
    return received


def _read_http_response_chunk(
    response: http.client.HTTPResponse,
    buffer: bytearray,
) -> int:
    """Читает штатный ответ, проверив размер очередного блока."""
    parsed_response = cast(_ParsedHTTPResponse, response)
    if not parsed_response.chunked:
        return parsed_response.readinto(buffer)

    chunk_left = parsed_response._get_chunk_left()
    if chunk_left is None:
        return 0
    if chunk_left < 0:
        raise DownloadError("сервер указал отрицательный размер блока")
    read_size = min(chunk_left, CHUNK_SIZE)
    return parsed_response.readinto(memoryview(buffer)[:read_size])


def download_once(
    url: str,
    timeout: float,
    progress: Progress,
    *,
    clock: Clock,
    opener: Opener | None = None,
) -> DownloadResult:
    """Полностью читает одно тело ответа и измеряет его."""
    active_opener = opener if opener is not None else make_opener()
    request = Request(url, headers={"Accept-Encoding": "identity"}, method="GET")
    started_at = clock()

    try:
        response = active_opener.open(request, timeout=timeout)
        with closing(response):
            received = _read_response_body(response, progress, started_at, clock)
    except HTTPError as error:
        error.close()
        raise DownloadError(f"HTTP {error.code}: {error.reason}") from error
    except RedirectError:
        raise
    except http.client.IncompleteRead as error:
        raise DownloadError("ответ оборвался до завершения тела") from error
    except http.client.HTTPException as error:
        raise DownloadError(f"ошибка протокола HTTP: {error}") from error
    except OverflowError as error:
        raise DownloadError("тайм-аут слишком велик для системы") from error
    except (URLError, TimeoutError, OSError) as error:
        reason = error.reason if isinstance(error, URLError) else error
        raise DownloadError(str(reason)) from error

    return DownloadResult(received, clock() - started_at)
