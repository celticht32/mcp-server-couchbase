"""
REST client for the Global Secondary Index (GSI) settings endpoint.

The GSI Settings API is served by the cluster manager on port 8091 (18091
for TLS) at ``/settings/indexes`` - distinct from the Index Service REST API
(port 9102) used by ``fetch_indexes_from_rest_api`` in ``index_utils``. GET
returns a JSON object of current settings; POST accepts
``application/x-www-form-urlencoded`` key-value pairs and leaves unspecified
parameters unchanged.

Verified against Couchbase Server docs (rest-api/get-settings-indexes,
post-settings-indexes, rest-index-service) 2026-07-02.

This helper reuses the connection-string host extraction and SSL
verification logic already present in ``index_utils`` so behavior (Capella
root CA handling, multi-host fallback, TLS detection) stays consistent with
the existing index REST path.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

import logging
from typing import Any

import httpx

from .constants import MCP_SERVER_NAME
from .index_utils import (
    _determine_ssl_verification,
    _extract_hosts_from_connection_string,
)

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.index_settings")

# Cluster-manager ports for the GSI Settings API (NOT the 9102 Index Service
# port used for /getIndexStatus).
_GSI_SETTINGS_PORT = 8091
_GSI_SETTINGS_TLS_PORT = 18091
_GSI_SETTINGS_PATH = "/settings/indexes"


def _settings_base(connection_string: str) -> tuple[str, int]:
    """Return (scheme, port) for the GSI settings endpoint."""
    is_tls = connection_string.lower().startswith("couchbases://")
    return ("https", _GSI_SETTINGS_TLS_PORT) if is_tls else ("http", _GSI_SETTINGS_PORT)


def get_gsi_settings(
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET the current GSI settings from the cluster.

    Tries each host in the connection string until one responds. Raises
    ``RuntimeError`` if all hosts fail.
    """
    return _request_gsi_settings(
        method="GET",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
    )


def set_gsi_settings(
    connection_string: str,
    username: str,
    password: str,
    params: dict[str, Any],
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST GSI settings (form-urlencoded) and return the resulting settings.

    ``params`` values are converted to the string forms the endpoint expects
    (booleans as lowercase ``true``/``false``). Only the supplied keys are
    sent; unspecified settings are left unchanged by the server. After a
    successful POST the current settings are read back and returned.
    """
    if not params:
        raise ValueError("params must contain at least one setting to update")

    form = {k: _form_value(v) for k, v in params.items() if v is not None}
    if not form:
        raise ValueError("params contained no non-null settings to update")

    _request_gsi_settings(
        method="POST",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        data=form,
    )
    # Read back so the caller sees the applied state.
    return get_gsi_settings(
        connection_string, username, password, ca_cert_path, timeout
    )


def _form_value(value: Any) -> str:
    """Render a value for the form-urlencoded body (bools lowercased)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _request_gsi_settings(
    *,
    method: str,
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None,
    timeout: int,
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Issue a GET/POST to /settings/indexes, trying each host in turn."""
    hosts = _extract_hosts_from_connection_string(connection_string)
    scheme, port = _settings_base(connection_string)
    verify_ssl = _determine_ssl_verification(connection_string, ca_cert_path)

    last_error: Exception | None = None
    with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
        for host in hosts:
            url = f"{scheme}://{host}:{port}{_GSI_SETTINGS_PATH}"
            try:
                logger.info(f"{method} {url}")
                if method == "GET":
                    resp = client.get(url, auth=(username, password))
                else:
                    resp = client.post(url, data=data, auth=(username, password))
                resp.raise_for_status()
                # GET returns settings JSON; POST returns an empty/!json body
                # on some versions, so only parse when there is content.
                if resp.content and resp.headers.get("content-type", "").startswith(
                    "application/json"
                ):
                    return resp.json()
                return {}
            except httpx.HTTPError as e:
                logger.warning(f"GSI settings {method} failed on {host}: {e}")
                last_error = e

    error_msg = f"GSI settings {method} failed on all hosts: {hosts}"
    if last_error:
        error_msg += f". Last error: {last_error}"
    logger.error(error_msg)
    raise RuntimeError(error_msg)
