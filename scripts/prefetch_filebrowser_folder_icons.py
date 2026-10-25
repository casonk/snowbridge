#!/usr/bin/env python3
"""Ask File Browser to prefetch icons using JSON auth or Caddy mTLS proxy auth."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import ssl
import stat
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

DEFAULT_CONFIG = Path.home() / ".config/snowbridge/folder-icons-scheduler.json"
MAX_CONFIG_BYTES = 16 * 1024
REQUEST_TIMEOUT_SECONDS = 20 * 60


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_config(path: Path) -> dict[str, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot safely open config {path}: {exc}") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"config is not a regular file: {path}")
        if metadata.st_uid != os.geteuid():
            raise ValueError(f"config is not owned by the current user: {path}")
        if metadata.st_mode & 0o077:
            raise ValueError(f"config must be owner-only (mode 0600): {path}")
        if metadata.st_size > MAX_CONFIG_BYTES:
            raise ValueError(f"config is larger than {MAX_CONFIG_BYTES} bytes: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            value = json.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if not isinstance(value, dict):
        raise ValueError("config must be a JSON object")
    auth_method = value.get("auth_method", "json")
    if auth_method == "json":
        valid_keys = {"base_url", "username", "password"}
        if "auth_method" in value:
            valid_keys.add("auth_method")
        if set(value) != valid_keys:
            raise ValueError("JSON auth config must contain base_url, username, and password")
        if not all(isinstance(value[key], str) and value[key] for key in valid_keys):
            raise ValueError("JSON auth values must be non-empty strings")
        _validate_local_url(value["base_url"])
    elif auth_method == "mtls-proxy":
        valid_keys = {
            "auth_method",
            "base_url",
            "username",
            "client_cert_path",
            "client_key_path",
            "server_ca_path",
        }
        if set(value) != valid_keys:
            raise ValueError(
                "mTLS config must contain the mTLS endpoint, username, and certificate paths"
            )
        if not all(isinstance(value[key], str) and value[key] for key in valid_keys):
            raise ValueError("mTLS config values must be non-empty strings")
        for key in ("client_cert_path", "client_key_path", "server_ca_path"):
            value[key] = str(Path(value[key]).expanduser())
        _validate_mtls_url(value["base_url"])
        _validate_owned_file(value["client_cert_path"], private=False)
        _validate_owned_file(value["client_key_path"], private=True)
        _validate_owned_file(value["server_ca_path"], private=False)
        if value["username"] != "snowbridge":
            raise ValueError(
                "mTLS proxy identity must match Caddy's configured File Browser user 'snowbridge'"
            )
    else:
        raise ValueError("auth_method must be 'json' or 'mtls-proxy'")
    return value


def _validate_local_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an http(s) URL on loopback")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("base_url has an invalid port") from exc
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("base_url must not contain a path prefix")
    hostname = parsed.hostname.lower()
    if hostname == "localhost":
        return
    try:
        if ipaddress.ip_address(hostname).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError("base_url must point to localhost or a loopback IP address")


def _validate_mtls_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "files.snowbridge.internal":
        raise ValueError("mTLS base_url must use https://files.snowbridge.internal")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base_url has an invalid port") from exc
    if port not in {None, 443}:
        raise ValueError("mTLS base_url must use the standard HTTPS port")
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "mTLS base_url must not include credentials, query, fragment, or path prefix"
        )


def _validate_owned_file(path_value: str, *, private: bool) -> None:
    path = Path(path_value).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot safely open certificate file {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"certificate path is not a regular file: {path}")
        if metadata.st_uid != os.geteuid():
            raise ValueError(f"certificate file is not owned by the current user: {path}")
        if private and metadata.st_mode & 0o077:
            raise ValueError(f"private key must be owner-only (mode 0600): {path}")
        if not private and metadata.st_mode & 0o022:
            raise ValueError(f"certificate file must not be group/other-writable: {path}")
        if metadata.st_size > 1024 * 1024:
            raise ValueError(f"certificate file is larger than 1 MiB: {path}")
    finally:
        os.close(descriptor)


def _request(
    url: str,
    data: bytes,
    token: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> bytes:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Auth"] = token
    request = Request(url, data=data, headers=headers, method="POST")
    try:
        opener = build_opener(_NoRedirect(), HTTPSHandler(context=ssl_context))
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read(1024 * 1024 + 1)
    except HTTPError as exc:
        raise RuntimeError(f"File Browser returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"could not reach the configured File Browser URL: {exc.reason}"
        ) from exc


def _mtls_ssl_context(config: dict[str, str]) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=config["server_ca_path"])
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict_flag:
        # The installed Wiring Harness Clockwork CA predates X.509 strict
        # validation and has no keyUsage extension. Keep normal chain, expiry,
        # and hostname checks while allowing this explicitly configured root.
        context.verify_flags &= ~strict_flag
    context.load_cert_chain(
        certfile=config["client_cert_path"],
        keyfile=config["client_key_path"],
    )
    return context


def prefetch(config: dict[str, str]) -> dict[str, object]:
    base_url = config["base_url"].rstrip("/")
    ssl_context = None
    if config.get("auth_method", "json") == "mtls-proxy":
        ssl_context = _mtls_ssl_context(config)
        login_data = b"{}"
    else:
        login_data = json.dumps(
            {"username": config["username"], "password": config["password"]}
        ).encode("utf-8")
    token = (
        _request(f"{base_url}/api/login", login_data, ssl_context=ssl_context)
        .decode("utf-8")
        .strip()
    )
    if not token or token.count(".") != 2:
        raise RuntimeError("File Browser login did not return an auth token")

    response = _request(f"{base_url}/api/foldericons/prefetch", b"{}", token, ssl_context)
    if len(response) > 1024 * 1024:
        raise RuntimeError("File Browser prefetch response exceeded 1 MiB")
    try:
        result = json.loads(response)
    except json.JSONDecodeError as exc:
        raise RuntimeError("File Browser returned invalid prefetch JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("File Browser returned an invalid prefetch response")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)

    try:
        result = prefetch(_read_config(args.config))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"folder icon prefetch failed: {exc}", file=sys.stderr)
        return 1

    print(
        "folder icon prefetch: "
        f"directories={result.get('directories_visited', 0)} "
        f"generated={result.get('folders_generated', 0)} "
        f"more={str(bool(result.get('more', False))).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
