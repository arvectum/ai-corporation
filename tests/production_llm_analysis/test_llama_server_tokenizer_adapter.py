import io
import json
from urllib.request import Request

import pytest

from scripts.r10_1.tokenize_via_llama_server import (
    TokenizerAdapterError,
    _validated_endpoint,
    count_tokens,
)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


def test_adapter_uses_only_loopback_tokenize_endpoint() -> None:
    captured: dict[str, object] = {}

    def opener(request: Request, *, timeout: float):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data or b"{}")
        return _Response(json.dumps({"tokens": [11, 22, 33]}).encode("utf-8"))

    value = count_tokens(
        "synthetic evidence",
        endpoint="http://127.0.0.1:8081/tokenize",
        timeout_seconds=5,
        opener=opener,
    )

    assert value == 3
    assert captured["url"] == "http://127.0.0.1:8081/tokenize"
    assert captured["timeout"] == 5
    assert captured["payload"] == {
        "content": "synthetic evidence",
        "add_special": False,
        "parse_special": True,
        "with_pieces": False,
    }


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:8081/tokenize",
        "http://localhost:8081/tokenize",
        "http://192.168.1.10:8081/tokenize",
        "http://127.0.0.1:8081/v1/chat/completions",
        "http://user:secret@127.0.0.1:8081/tokenize",
        "http://127.0.0.1:8081/tokenize?debug=1",
    ],
)
def test_adapter_rejects_noncanonical_or_nonloopback_endpoint(endpoint: str) -> None:
    with pytest.raises(TokenizerAdapterError, match="tokenizer_endpoint_not_loopback"):
        _validated_endpoint(endpoint)


def test_adapter_rejects_malformed_response_without_exposing_body() -> None:
    def opener(request: Request, *, timeout: float):
        return _Response(b'{"error":"secret diagnostic"}')

    with pytest.raises(TokenizerAdapterError, match="tokenizer_response_invalid") as error:
        count_tokens(
            "synthetic evidence",
            endpoint="http://127.0.0.1:8081/tokenize",
            opener=opener,
        )

    assert "secret diagnostic" not in str(error.value)
