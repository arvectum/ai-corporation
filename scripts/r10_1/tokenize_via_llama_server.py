#!/usr/bin/env python3
"""Count tokens through a loopback llama.cpp server without model reloads.

The adapter reads the text from stdin and prints the exact output contract used
by ``CommandTokenCounter``.  It calls only llama.cpp's non-generating
``/tokenize`` endpoint; chat/completion endpoints are never used.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class TokenizerAdapterError(RuntimeError):
    """Sanitized local tokenizer adapter failure."""


_ALLOWED_HOSTS = {"127.0.0.1", "::1"}
_DEFAULT_TIMEOUT_SECONDS = 30.0


def _validated_endpoint(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/tokenize"
        or parsed.port is None
    ):
        raise TokenizerAdapterError("tokenizer_endpoint_not_loopback")
    return value.strip()


def count_tokens(
    text: str,
    *,
    endpoint: str,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urlopen,
) -> int:
    """Return exact token count from a loaded loopback llama.cpp model."""

    if timeout_seconds <= 0:
        raise TokenizerAdapterError("tokenizer_timeout_invalid")
    target = _validated_endpoint(endpoint)
    payload = json.dumps(
        {
            "content": text,
            "add_special": False,
            "parse_special": True,
            "with_pieces": False,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        target,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            body = response.read()
    except TimeoutError as exc:
        raise TokenizerAdapterError("tokenizer_request_timeout") from exc
    except (HTTPError, URLError, OSError) as exc:
        raise TokenizerAdapterError("tokenizer_request_failed") from exc

    try:
        decoded = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise TokenizerAdapterError("tokenizer_response_invalid") from exc
    tokens = decoded.get("tokens") if isinstance(decoded, dict) else None
    if not isinstance(tokens, list) or any(not isinstance(token, int) for token in tokens):
        raise TokenizerAdapterError("tokenizer_response_invalid")
    return len(tokens)


def main() -> int:
    endpoint = os.environ.get("ARV003_LLAMA_TOKENIZER_URL", "")
    if not endpoint:
        print("tokenizer_endpoint_missing", file=sys.stderr)
        return 2
    try:
        text = sys.stdin.buffer.read().decode("utf-8")
        value = count_tokens(text, endpoint=endpoint)
    except (UnicodeDecodeError, TokenizerAdapterError):
        print("tokenizer_adapter_failed", file=sys.stderr)
        return 2
    print(f"Total number of tokens: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
