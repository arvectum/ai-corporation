from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ProviderTimeoutError(TimeoutError):
    """A provider attempt exceeded its configured timeout."""


class ProviderTransientError(ConnectionError):
    """A retryable provider or network failure occurred."""


class ProviderPermanentError(ConnectionError):
    """A non-retryable provider rejection occurred."""


class ProviderBudgetExceededError(RuntimeError):
    """A provider call or retry would exceed an analysis-wide budget."""


class InvalidProviderResponseError(ValueError):
    """The provider returned a response outside the versioned contract."""


@dataclass(frozen=True)
class HTTPRequest:
    url: str
    body: bytes
    headers: Mapping[str, str] = field(repr=False)
    timeout_ms: int
    method: str = "POST"


@dataclass(frozen=True)
class HTTPResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class HTTPClient(Protocol):
    def send(self, request: HTTPRequest) -> HTTPResponse: ...


class UrllibHTTPClient:
    """Minimal HTTP boundary. Production credentials never enter raised errors."""

    def send(self, request: HTTPRequest) -> HTTPResponse:
        urllib_request = Request(
            url=request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with urlopen(urllib_request, timeout=request.timeout_ms / 1000) as response:
                return HTTPResponse(
                    status_code=int(getattr(response, "status", response.getcode())),
                    body=response.read(),
                    headers=dict(response.headers.items()),
                )
        except HTTPError as exc:
            return HTTPResponse(
                status_code=int(exc.code),
                body=exc.read(),
                headers=dict(exc.headers.items()) if exc.headers is not None else {},
            )
        except (TimeoutError, socket.timeout) as exc:
            raise ProviderTimeoutError("provider_timeout") from None
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ProviderTimeoutError("provider_timeout") from None
            raise ProviderTransientError("provider_connection_failed") from None
        except OSError as exc:
            raise ProviderTransientError("provider_connection_failed") from None
