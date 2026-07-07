"""
REST client for XDCR (Cross-Datacenter Replication) admin endpoints.

XDCR admin is split across three endpoint families:

- ``/pools/default/remoteClusters[/{name}]`` — remote-cluster references
  (create/list/update/delete on port 8091/18091).
- ``/controller/createReplication`` and ``/controller/cancelXDCR/{id}`` —
  replication lifecycle actions.
- ``/settings/replications[/{id}]`` — per-replication and cluster-wide
  replication settings (throttling, filter expressions, priority, pause
  state). GET returns JSON; POST accepts form-urlencoded pairs.

Verified against Couchbase Server docs (rest-api/rest-xdcr-*, xdcr-managing)
2026-07-06.

This helper reuses the connection-string host extraction and SSL
verification logic already present in ``index_utils`` so behavior (Capella
root CA handling, multi-host fallback, TLS detection) stays consistent
with the existing REST paths in ``index_settings.py`` and
``bucket_admin_rest.py``.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

import json
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from .constants import MCP_SERVER_NAME
from .index_utils import (
    _determine_ssl_verification,
    _extract_hosts_from_connection_string,
)

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.xdcr_rest")

# Cluster-manager ports (same as GSI settings and bucket admin).
_MGMT_PORT = 8091
_MGMT_TLS_PORT = 18091

_REMOTE_CLUSTERS_PATH = "/pools/default/remoteClusters"
_CREATE_REPLICATION_PATH = "/controller/createReplication"
_CANCEL_REPLICATION_PATH = "/controller/cancelXDCR"  # + /{replication_id}
_REPLICATION_SETTINGS_PATH = "/settings/replications"  # + /{replication_id}

# Remote-cluster name: same charset as bucket name per Couchbase docs
# (letters, digits, and ``.``, ``-``, ``_``, ``%``). Reject anything else
# BEFORE it enters a URL path.
_REMOTE_CLUSTER_NAME_RE = re.compile(r"^[A-Za-z0-9._\-%]{1,100}$")

# Replication ID: cluster-generated as ``{uuid}/{sourceBucket}/{targetBucket}``.
# Contains slashes, so this validator rejects control chars and things that
# would break URL parsing, not the slashes themselves. Each segment is
# path-encoded individually before use.
_REPLICATION_ID_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._\-%]{1,100}$")


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------


def assert_remote_cluster_name(name: str) -> None:
    """Reject remote-cluster names that don't match Couchbase's charset."""
    if not isinstance(name, str) or not _REMOTE_CLUSTER_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid remote-cluster name {name!r}. Names must be 1-100 "
            "chars of [A-Za-z0-9._%-]."
        )


def assert_replication_id(replication_id: str) -> None:
    """Reject replication IDs that don't fit the ``uuid/src/tgt`` shape.

    Each of the three slash-separated segments is checked against
    ``_REPLICATION_ID_SEGMENT_RE`` (letters, digits, ``.``, ``-``, ``_``,
    ``%``). Raises ``ValueError`` on any deviation.
    """
    if not isinstance(replication_id, str) or "/" not in replication_id:
        raise ValueError(
            f"Invalid replication ID {replication_id!r}. Expected the "
            "cluster-generated ``uuid/sourceBucket/targetBucket`` form."
        )
    segments = replication_id.split("/")
    if len(segments) != 3:
        raise ValueError(
            f"Invalid replication ID {replication_id!r}. Expected exactly "
            f"three segments (uuid/src/tgt), got {len(segments)}."
        )
    for i, seg in enumerate(segments):
        if not _REPLICATION_ID_SEGMENT_RE.fullmatch(seg):
            raise ValueError(
                f"Invalid replication ID segment {seg!r} at index {i}. "
                "Each segment must be 1-100 chars of [A-Za-z0-9._%-]."
            )


def _encode_path_segment(value: str) -> str:
    """URL-encode a value for use as a REST path segment.

    Same helper pattern as ``bucket_admin_rest._encode_path_segment``:
    ``%`` is a valid character in Couchbase names, and raw interpolation
    could produce path-confusion vectors if a name were, e.g., ``%2f%2e%2e``.
    Encoding with ``safe=""`` produces ``%252f%252e%252e``, which the
    server treats as an opaque identifier.
    """
    return quote(value, safe="")


def _encoded_replication_id(replication_id: str) -> str:
    """Encode a replication ID's three segments individually so slashes
    between segments survive but slashes WITHIN a segment (never legal in
    Couchbase names) would be double-encoded."""
    assert_replication_id(replication_id)
    return "/".join(_encode_path_segment(s) for s in replication_id.split("/"))


# --------------------------------------------------------------------------
# Small internals shared by every REST call
# --------------------------------------------------------------------------


def _mgmt_base(connection_string: str) -> tuple[str, int]:
    """Return (scheme, port) for the cluster-manager endpoint."""
    is_tls = connection_string.lower().startswith("couchbases://")
    return ("https", _MGMT_TLS_PORT) if is_tls else ("http", _MGMT_PORT)


def _form_value(value: Any) -> str:
    """Render a value for the form-urlencoded body.

    XDCR endpoints follow the same convention as bucket admin: bools as
    lowercase ``true``/``false``, ints bare, dicts as JSON (some XDCR
    settings take nested objects), other values as str().
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value)


def _request(
    *,
    method: str,
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None,
    timeout: int,
    path: str,
    data: dict[str, str] | None = None,
    expected_ok: tuple[int, ...] = (200, 202),
) -> dict[str, Any]:
    """Issue a REST request to the cluster manager, trying each host in turn.

    On success returns the parsed JSON body when present, or an empty dict
    (many XDCR endpoints return an empty body on 200/202). Raises
    ``RuntimeError`` if every host fails.
    """
    hosts = _extract_hosts_from_connection_string(connection_string)
    scheme, port = _mgmt_base(connection_string)
    verify_ssl = _determine_ssl_verification(connection_string, ca_cert_path)

    last_error: Exception | None = None
    with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
        for host in hosts:
            url = f"{scheme}://{host}:{port}{path}"
            try:
                logger.info(f"{method} {url}")
                if method == "GET":
                    resp = client.get(url, auth=(username, password))
                elif method == "DELETE":
                    resp = client.delete(url, auth=(username, password))
                elif method == "POST":
                    resp = client.post(url, data=data, auth=(username, password))
                else:
                    raise ValueError(f"unsupported HTTP method: {method}")
                if resp.status_code not in expected_ok:
                    resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if resp.content and content_type.startswith("application/json"):
                    return resp.json()
                return {}
            except httpx.HTTPError as e:
                logger.warning(f"XDCR {method} failed on {host}: {e}")
                last_error = e

    error_msg = f"XDCR {method} {path} failed on all hosts: {hosts}"
    if last_error:
        error_msg += f". Last error: {last_error}"
    logger.error(error_msg)
    raise RuntimeError(error_msg)


# --------------------------------------------------------------------------
# Remote-cluster REST wrappers
# --------------------------------------------------------------------------


def create_remote_cluster_rest(
    connection_string: str,
    username: str,
    password: str,
    form: dict[str, Any],
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST /pools/default/remoteClusters."""
    body = {k: _form_value(v) for k, v in form.items() if v is not None}
    return _request(
        method="POST",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=_REMOTE_CLUSTERS_PATH,
        data=body,
    )


def update_remote_cluster_rest(
    connection_string: str,
    username: str,
    password: str,
    name: str,
    form: dict[str, Any],
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST /pools/default/remoteClusters/{name}."""
    assert_remote_cluster_name(name)
    body = {k: _form_value(v) for k, v in form.items() if v is not None}
    return _request(
        method="POST",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=f"{_REMOTE_CLUSTERS_PATH}/{_encode_path_segment(name)}",
        data=body,
    )


def delete_remote_cluster_rest(
    connection_string: str,
    username: str,
    password: str,
    name: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """DELETE /pools/default/remoteClusters/{name}."""
    assert_remote_cluster_name(name)
    return _request(
        method="DELETE",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=f"{_REMOTE_CLUSTERS_PATH}/{_encode_path_segment(name)}",
    )


def list_remote_clusters_rest(
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> list[dict[str, Any]] | dict[str, Any]:
    """GET /pools/default/remoteClusters — returns a list of remote-cluster refs."""
    return _request(
        method="GET",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=_REMOTE_CLUSTERS_PATH,
    )


# --------------------------------------------------------------------------
# Replication REST wrappers
# --------------------------------------------------------------------------


def create_replication_rest(
    connection_string: str,
    username: str,
    password: str,
    form: dict[str, Any],
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST /controller/createReplication."""
    body = {k: _form_value(v) for k, v in form.items() if v is not None}
    return _request(
        method="POST",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=_CREATE_REPLICATION_PATH,
        data=body,
    )


def delete_replication_rest(
    connection_string: str,
    username: str,
    password: str,
    replication_id: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """DELETE /controller/cancelXDCR/{replication_id}."""
    return _request(
        method="DELETE",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=f"{_CANCEL_REPLICATION_PATH}/{_encoded_replication_id(replication_id)}",
    )


def get_replication_settings_rest(
    connection_string: str,
    username: str,
    password: str,
    replication_id: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET /settings/replications/{replication_id}."""
    return _request(
        method="GET",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=f"{_REPLICATION_SETTINGS_PATH}/{_encoded_replication_id(replication_id)}",
    )


def update_replication_settings_rest(
    connection_string: str,
    username: str,
    password: str,
    replication_id: str,
    form: dict[str, Any],
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST /settings/replications/{replication_id}."""
    if not form:
        raise ValueError("form must contain at least one setting to update")
    body = {k: _form_value(v) for k, v in form.items() if v is not None}
    return _request(
        method="POST",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=f"{_REPLICATION_SETTINGS_PATH}/{_encoded_replication_id(replication_id)}",
        data=body,
    )


def list_replications_rest(
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """GET /pools/default/tasks — filtered to XDCR tasks only.

    The tasks endpoint returns rebalance status, compaction tasks, and
    XDCR replications in a single array. We filter to ``type='xdcr'`` for
    the caller.
    """
    result = _request(
        method="GET",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path="/pools/default/tasks",
    )
    if isinstance(result, list):
        return [t for t in result if isinstance(t, dict) and t.get("type") == "xdcr"]
    return []
