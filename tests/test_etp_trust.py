from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from src.shared.config.settings import Settings
from src.shared.network.etp_trust import (
    Authority,
    ETPTrustConfigurationError,
    HostPolicy,
    TrustPolicy,
    build_ssl_context,
    policy_from_settings,
    resolve_host_policy,
    should_bypass_proxy,
    validate_ca_file,
)
from src.shared.network.http_client import create_urllib_context


def _ca(tmp_path: Path) -> Path:
    key = tmp_path / "ca.key"
    cert = tmp_path / "ca.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-nodes",
            "-subj",
            "/CN=Test CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
        ],
        check=True,
        capture_output=True,
    )
    return cert


def _policy(cert: Path, enabled: bool = True) -> TrustPolicy:
    return TrustPolicy(
        enabled=enabled,
        authorities={"test": Authority("test", cert, _der_sha(cert))},
        hosts=(
            HostPolicy(".zakupki.gov.ru", "test", True),
            HostPolicy("zakupki.gov.ru", "test", True),
        ),
    )


def _der_sha(cert: Path) -> str:
    command = ["openssl", "x509", "-in", str(cert), "-outform", "DER"]
    if cert.suffix == ".der":
        command[2:2] = ["-inform", "DER"]
    der = subprocess.run(command, check=True, capture_output=True).stdout
    return hashlib.sha256(der).hexdigest().upper()


def test_suffix_matching_is_boundary_safe(tmp_path: Path):
    policy = _policy(_ca(tmp_path))
    assert resolve_host_policy("api.zakupki.gov.ru", policy)
    assert resolve_host_policy("evil-zakupki.gov.ru.example.com", policy) is None


def test_unknown_host_keeps_strict_default_and_no_proxy_bypass(tmp_path: Path):
    policy = _policy(_ca(tmp_path))
    context = build_ssl_context("example.com", policy)
    assert context.verify_mode.value == 2
    assert context.check_hostname is True
    assert should_bypass_proxy("example.com", policy) is False


def test_allowed_host_gets_extra_ca_and_direct_policy(tmp_path: Path):
    cert = _ca(tmp_path)
    context = build_ssl_context("zakupki.gov.ru", _policy(cert))
    assert context.verify_mode.value == 2
    assert context.check_hostname is True
    assert should_bypass_proxy("api.zakupki.gov.ru", _policy(cert)) is True


def test_system_authority_is_strict_and_allowlisted(tmp_path: Path):
    policy = TrustPolicy(
        enabled=True,
        authorities={"macos_system": Authority("macos_system", type="system")},
        hosts=(HostPolicy("zakupki.gov.ru", "macos_system", True),),
    )
    context = build_ssl_context("zakupki.gov.ru", policy)
    assert context.verify_mode.value == 2
    assert context.check_hostname is True
    assert should_bypass_proxy("zakupki.gov.ru", policy) is True
    assert should_bypass_proxy("example.com", policy) is False


def test_fingerprint_mismatch_and_missing_file_fail_closed(tmp_path: Path):
    cert = _ca(tmp_path)
    with pytest.raises(ETPTrustConfigurationError):
        validate_ca_file(Authority("test", cert, "0" * 64))
    with pytest.raises(ETPTrustConfigurationError):
        validate_ca_file(Authority("test", tmp_path / "missing.pem", "0" * 64))


def test_pem_and_der_share_certificate_fingerprint_but_not_file_hash(tmp_path: Path):
    pem = _ca(tmp_path)
    der = tmp_path / "ca.der"
    der.write_bytes(
        subprocess.run(
            ["openssl", "x509", "-in", str(pem), "-outform", "DER"],
            check=True,
            capture_output=True,
        ).stdout
    )
    assert _der_sha(pem) == _der_sha(der)
    assert (
        hashlib.sha256(pem.read_bytes()).hexdigest()
        != hashlib.sha256(der.read_bytes()).hexdigest()
    )
    authority = Authority("test", der, _der_sha(pem))
    assert validate_ca_file(authority) == der


def test_policy_from_settings_resolves_relative_path_without_environment(
    tmp_path: Path, monkeypatch
):
    policy_file = tmp_path / "runtime" / "policy.yml"
    policy_file.parent.mkdir()
    policy_file.write_text(
        "enabled: true\nproxy_bypass_enabled: true\nhosts:\n  example.test:\n    direct_connection: true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(
        tmp_path / "other" if (tmp_path / "other").mkdir() is None else tmp_path
    )
    settings = Settings(
        etp_tls_policy_path="policy.yml",
        etp_tls_enabled=True,
        etp_tls_fail_closed=True,
        etp_proxy_bypass_enabled=True,
    )
    policy = policy_from_settings(settings, base_dir=policy_file.parent)
    assert policy.enabled is True
    assert resolve_host_policy("example.test", policy) is not None


def test_policy_from_settings_rejects_symlinked_policy(tmp_path: Path):
    real = tmp_path / "real.yml"
    real.write_text("enabled: true\nhosts: {}\n", encoding="utf-8")
    link = tmp_path / "link.yml"
    link.symlink_to(real)
    settings = Settings(etp_tls_policy_path="link.yml", etp_tls_enabled=True)
    with pytest.raises(ETPTrustConfigurationError):
        policy_from_settings(settings, base_dir=tmp_path)


def test_injected_http_policy_does_not_read_environment(monkeypatch):
    import src.shared.network.http_client as module

    monkeypatch.setattr(
        module,
        "policy_from_environment",
        lambda: (_ for _ in ()).throw(AssertionError()),
    )
    policy = TrustPolicy(
        enabled=True,
        proxy_bypass_enabled=True,
        hosts=(HostPolicy("example.test", direct_connection=True),),
    )
    context, bypass = create_urllib_context("https://example.test/path", policy=policy)
    assert context.minimum_version == __import__("ssl").TLSVersion.TLSv1_2
    assert bypass is True
