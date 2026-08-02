import subprocess
import tempfile
from pathlib import Path

from src.tender_research import document_text_extractor as extractor
from src.tender_research.document_text_extractor import extract_text


def test_txt_extraction():
    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
        f.write("Hello world\nTest line 2")
        path = f.name
    status, text = extract_text(path)
    assert status == "extracted"
    assert "Hello world" in text
    Path(path).unlink()


def test_unsupported_extension():
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(b"fake-image-data")
        path = f.name
    status, text = extract_text(path)
    assert status == "unsupported"
    Path(path).unlink()


def test_empty_file():
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        path = f.name
    status, text = extract_text(path)
    assert status == "empty"
    assert text == ""
    Path(path).unlink()


def test_legacy_doc_extraction_uses_fixed_textutil_boundary(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "requirements.doc"
    source.write_bytes(b"legacy-word-binary")
    converter = tmp_path / "textutil"
    converter.write_bytes(b"mock")
    calls: list[tuple[list[str], dict]] = []

    def fake_run(args, **kwargs):
        calls.append((list(args), dict(kwargs)))
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="Требования к составу заявки".encode(),
            stderr=b"",
        )

    monkeypatch.setattr(extractor, "_MACOS_TEXTUTIL", converter)
    monkeypatch.setattr(extractor.subprocess, "run", fake_run)

    status, text = extractor.extract_text(str(source), max_chars=12)

    assert status == extractor.EXTRACTED_STATUS
    assert text == "Требования к"
    assert calls == [
        (
            [
                str(converter),
                "-convert",
                "txt",
                "-stdout",
                "-encoding",
                "UTF-8",
                str(source),
            ],
            {
                "capture_output": True,
                "check": False,
                "timeout": extractor._TEXTUTIL_TIMEOUT_SECONDS,
            },
        )
    ]


def test_legacy_doc_conversion_failure_is_recognized_but_empty(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "requirements.doc"
    source.write_bytes(b"legacy-word-binary")
    converter = tmp_path / "textutil"
    converter.write_bytes(b"mock")

    monkeypatch.setattr(extractor, "_MACOS_TEXTUTIL", converter)
    monkeypatch.setattr(
        extractor.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout=b"",
            stderr=b"conversion failed",
        ),
    )

    status, text = extractor.extract_text(str(source))

    assert status == extractor.EMPTY_STATUS
    assert text == ""


def test_legacy_doc_without_native_converter_fails_closed(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "requirements.doc"
    source.write_bytes(b"legacy-word-binary")
    monkeypatch.setattr(extractor, "_MACOS_TEXTUTIL", tmp_path / "missing-textutil")

    status, text = extractor.extract_text(str(source))

    assert status == extractor.EMPTY_STATUS
    assert text == ""
