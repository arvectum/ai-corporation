from __future__ import annotations

import hashlib
import os
import re
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import truststore
import yaml

from src.shared.config.settings import Settings


class ETPTrustConfigurationError(ValueError):
    """Raised when an ETP trust policy cannot be validated safely."""


@dataclass(frozen=True)
class Authority:
    name: str
    file: Path | None = None
    certificate_sha256: tuple[str, ...] | None = None
    type: str = "file"


@dataclass(frozen=True)
class HostPolicy:
    hostname: str
    authority: str | None = None
    direct_connection: bool = False


@dataclass(frozen=True)
class TrustPolicy:
    enabled: bool = False
    fail_closed: bool = True
    proxy_bypass_enabled: bool = True
    authorities: dict[str, Authority] | None = None
    hosts: tuple[HostPolicy, ...] = ()


def _normalise_host(hostname: str) -> str:
    host = hostname.strip().lower().rstrip(".")
    if not host or "/" in host or any(ch.isspace() for ch in host):
        raise ETPTrustConfigurationError(f"Invalid hostname: {hostname!r}")
    return host


def _matches(rule: str, hostname: str) -> bool:
    rule = _normalise_host(rule)
    hostname = _normalise_host(hostname)
    if rule.startswith("."):
        suffix = rule[1:]
        return hostname.endswith(rule) and hostname != suffix
    return hostname == rule


def load_trust_policy(path: str | os.PathLike[str] | None) -> TrustPolicy:
    if not path:
        return TrustPolicy()
    policy_path = Path(path).expanduser()
    if not policy_path.is_file():
        raise ETPTrustConfigurationError(f"Trust policy does not exist: {policy_path}")
    try:
        raw = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ETPTrustConfigurationError(f"Cannot read trust policy: {exc}") from exc
    if not isinstance(raw, dict):
        raise ETPTrustConfigurationError("Trust policy must be a mapping")
    defaults = raw.get("defaults") or {}
    authorities: dict[str, Authority] = {}
    for name, value in (raw.get("authorities") or {}).items():
        authority_type = (
            str(value.get("type", "file")) if isinstance(value, dict) else "file"
        )
        fingerprint_raw = (
            value.get("certificate_sha256") or value.get("sha256")
            if isinstance(value, dict)
            else None
        )
        if authority_type == "system":
            authorities[str(name)] = Authority(name=str(name), type="system")
            continue
        if not isinstance(value, dict) or not value.get("file") or not fingerprint_raw:
            raise ETPTrustConfigurationError(
                f"Authority {name!r} requires file and certificate_sha256"
            )
        fingerprint_values = (
            fingerprint_raw if isinstance(fingerprint_raw, list) else [fingerprint_raw]
        )
        fingerprints = tuple(
            str(item).replace(":", "").upper() for item in fingerprint_values
        )
        if not fingerprints or any(
            not re.fullmatch(r"[0-9A-F]{64}", item) for item in fingerprints
        ):
            raise ETPTrustConfigurationError(f"Authority {name!r} has invalid SHA-256")
        authorities[str(name)] = Authority(
            name=str(name),
            file=(policy_path.parent / str(value["file"])).resolve(),
            certificate_sha256=fingerprints,
            type="file",
        )
    hosts = tuple(
        HostPolicy(
            hostname=_normalise_host(str(host)),
            authority=str(value.get("authority"))
            if isinstance(value, dict) and value.get("authority")
            else None,
            direct_connection=bool(value.get("direct_connection", False))
            if isinstance(value, dict)
            else False,
        )
        for host, value in (raw.get("hosts") or {}).items()
    )
    return TrustPolicy(
        enabled=bool(raw.get("enabled", False)),
        fail_closed=bool(defaults.get("fail_closed", True)),
        proxy_bypass_enabled=bool(raw.get("proxy_bypass_enabled", True)),
        authorities=authorities,
        hosts=hosts,
    )


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            if current.is_symlink():
                raise ETPTrustConfigurationError("Trust policy path contains a symlink")
        except OSError as exc:
            raise ETPTrustConfigurationError(
                "Trust policy path cannot be inspected"
            ) from exc


def policy_from_settings(
    settings: Settings, *, base_dir: Path | None = None
) -> TrustPolicy:
    """Build an ETP policy from explicit settings without consulting the environment."""
    configured_path = settings.etp_tls_policy_path
    if not configured_path:
        if settings.etp_tls_enabled:
            raise ETPTrustConfigurationError("TLS policy is required")
        return TrustPolicy(
            enabled=False,
            fail_closed=settings.etp_tls_fail_closed,
            proxy_bypass_enabled=settings.etp_proxy_bypass_enabled,
        )

    policy_path = Path(configured_path).expanduser()
    if not policy_path.is_absolute():
        policy_path = (base_dir or Path.cwd()) / policy_path
    _reject_symlink_components(policy_path)
    try:
        loaded = load_trust_policy(policy_path)
    except ETPTrustConfigurationError as exc:
        raise ETPTrustConfigurationError("TLS policy file is invalid") from exc
    if settings.etp_tls_enabled != loaded.enabled:
        raise ETPTrustConfigurationError("TLS policy enabled state is inconsistent")
    if not settings.etp_tls_enabled:
        return TrustPolicy(
            enabled=False,
            fail_closed=settings.etp_tls_fail_closed,
            proxy_bypass_enabled=settings.etp_proxy_bypass_enabled,
            authorities=loaded.authorities,
            hosts=loaded.hosts,
        )
    return TrustPolicy(
        enabled=True,
        fail_closed=settings.etp_tls_fail_closed,
        proxy_bypass_enabled=settings.etp_proxy_bypass_enabled,
        authorities=loaded.authorities,
        hosts=loaded.hosts,
    )


def resolve_host_policy(hostname: str, policy: TrustPolicy) -> HostPolicy | None:
    host = _normalise_host(hostname)
    matches = [item for item in policy.hosts if _matches(item.hostname, host)]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item.hostname))


def validate_ca_file(authority: Authority) -> Path:
    path = authority.file
    if path is None or authority.certificate_sha256 is None:
        raise ETPTrustConfigurationError(
            f"File authority {authority.name} is incomplete"
        )
    if not path.is_file() or path.is_symlink():
        raise ETPTrustConfigurationError(f"CA file is missing or symlinked: {path}")
    expected = (
        (authority.certificate_sha256,)
        if isinstance(authority.certificate_sha256, str)
        else authority.certificate_sha256
    )
    raw = path.read_bytes()
    pem_blocks = re.findall(
        rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", raw, re.DOTALL
    )
    normalized: list[tuple[bytes, bytes]] = []
    try:
        if pem_blocks:
            for pem in pem_blocks:
                der = subprocess.run(
                    ["openssl", "x509", "-inform", "PEM", "-outform", "DER"],
                    input=pem,
                    check=True,
                    capture_output=True,
                    timeout=5,
                ).stdout
                normalized.append((der, pem))
        else:
            der = subprocess.run(
                [
                    "openssl",
                    "x509",
                    "-inform",
                    "DER",
                    "-in",
                    str(path),
                    "-outform",
                    "DER",
                ],
                check=True,
                capture_output=True,
                timeout=5,
            ).stdout
            pem = subprocess.run(
                [
                    "openssl",
                    "x509",
                    "-inform",
                    "DER",
                    "-in",
                    str(path),
                    "-outform",
                    "PEM",
                ],
                check=True,
                capture_output=True,
                timeout=5,
            ).stdout
            normalized.append((der, pem))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ETPTrustConfigurationError(
            f"Cannot decode CA certificate: {exc}"
        ) from exc
    digests = []
    for der, pem in normalized:
        text = subprocess.run(
            ["openssl", "x509", "-inform", "PEM", "-noout", "-text"],
            input=pem,
            check=True,
            capture_output=True,
            timeout=5,
        ).stdout.decode("utf-8", errors="replace")
        if "CA:TRUE" not in text:
            raise ETPTrustConfigurationError(
                f"Certificate is not marked as a CA: {path}"
            )
        digests.append(hashlib.sha256(der).hexdigest().upper())
    if set(digests) != set(expected) or len(digests) != len(expected):
        raise ETPTrustConfigurationError(
            f"SHA-256 mismatch for authority {authority.name}"
        )
    try:
        info = ssl._ssl._test_decode_cert(str(path))
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=str(path))
    except ssl.SSLError:
        try:
            pem = subprocess.run(
                [
                    "openssl",
                    "x509",
                    "-inform",
                    "DER",
                    "-in",
                    str(path),
                    "-outform",
                    "PEM",
                ],
                check=True,
                capture_output=True,
                timeout=5,
            ).stdout
            with tempfile.NamedTemporaryFile(suffix=".pem") as converted:
                converted.write(pem)
                converted.flush()
                info = ssl._ssl._test_decode_cert(converted.name)
                context = ssl.create_default_context()
                context.load_verify_locations(cafile=converted.name)
        except (OSError, ssl.SSLError, subprocess.SubprocessError, ValueError) as exc:
            raise ETPTrustConfigurationError(
                f"Invalid CA certificate {path}: {exc}"
            ) from exc
    except (OSError, ValueError) as exc:
        raise ETPTrustConfigurationError(
            f"Invalid CA certificate {path}: {exc}"
        ) from exc
    if not info.get("notAfter"):
        raise ETPTrustConfigurationError(f"CA certificate has no expiry: {path}")
    try:
        result = subprocess.run(
            ["openssl", "x509", "-in", str(path), "-noout", "-text"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.CalledProcessError:
        result = subprocess.run(
            ["openssl", "x509", "-inform", "DER", "-in", str(path), "-noout", "-text"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ETPTrustConfigurationError(
            f"Cannot inspect CA certificate: {exc}"
        ) from exc
    if "CA:TRUE" not in result.stdout:
        raise ETPTrustConfigurationError(f"Certificate is not marked as a CA: {path}")
    return path


def build_ssl_context(hostname: str, policy: TrustPolicy) -> ssl.SSLContext:
    context = ssl.create_default_context()
    host_policy = resolve_host_policy(hostname, policy)
    if host_policy and host_policy.authority:
        if not policy.enabled:
            raise ETPTrustConfigurationError("ETP trust policy is disabled")
        authority = (policy.authorities or {}).get(host_policy.authority)
        if authority is None:
            raise ETPTrustConfigurationError(
                f"Unknown authority: {host_policy.authority}"
            )
        if authority.type == "system":
            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        else:
            context.load_verify_locations(cafile=str(validate_ca_file(authority)))
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def should_bypass_proxy(hostname: str, policy: TrustPolicy) -> bool:
    host_policy = resolve_host_policy(hostname, policy)
    return bool(
        policy.enabled
        and policy.proxy_bypass_enabled
        and host_policy
        and host_policy.direct_connection
    )


def policy_from_environment() -> TrustPolicy:
    enabled = os.getenv("ARVECTUM_ETP_TLS_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    path = os.getenv("ARVECTUM_ETP_TLS_POLICY_PATH")
    policy = load_trust_policy(path) if path else TrustPolicy()
    return TrustPolicy(
        enabled=enabled and policy.enabled,
        fail_closed=os.getenv("ARVECTUM_ETP_TLS_FAIL_CLOSED", "true").lower()
        not in {"0", "false", "no"},
        proxy_bypass_enabled=os.getenv(
            "ARVECTUM_ETP_PROXY_BYPASS_ENABLED", "true"
        ).lower()
        not in {"0", "false", "no"},
        authorities=policy.authorities,
        hosts=policy.hosts,
    )
