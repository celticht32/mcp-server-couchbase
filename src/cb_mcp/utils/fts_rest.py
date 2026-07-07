"""
REST client for FTS (Full-Text Search) index management endpoints.

FTS admin runs on a dedicated service port (8094 for HTTP, 18094 for TLS).
The endpoints are:

- ``/api/index`` — GET lists all indexes; PUT ``/api/index/{name}`` creates
  or updates an index; DELETE ``/api/index/{name}`` removes one.
- ``/api/index/{name}`` — GET returns the index definition.
- ``/api/index/{name}/count`` — GET returns document count.
- ``/api/index/{name}/ingestControl/{op}`` — POST pause / resume ingestion.
- ``/api/index/{name}/queryControl/{op}`` — POST allow / disallow queries.
- ``/api/analyzeDoc/{name}`` — POST returns how a document would be
  analyzed against the given index's mapping.

Verified against Couchbase Server FTS REST API docs 2026-07-06.

Reuses the connection-string host extraction and SSL verification logic
already present in ``index_utils`` so behavior (Capella root CA handling,
multi-host fallback, TLS detection) stays consistent with the existing
REST paths in ``index_settings.py``, ``bucket_admin_rest.py``, and
``xdcr_rest.py``.

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

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.fts_rest")

# Search service ports (distinct from cluster manager 8091/18091 and Index
# Service 9102).
_FTS_PORT = 8094
_FTS_TLS_PORT = 18094

_INDEX_PATH = "/api/index"  # + /{name}
_ANALYZE_DOC_PATH = "/api/analyzeDoc"  # + /{name}

# Index name: Couchbase FTS accepts letters, digits, dot, dash, underscore,
# and percent. No slashes (path separator), no whitespace, no control chars.
_INDEX_NAME_RE = re.compile(r"^[A-Za-z0-9._\-%]{1,200}$")

# Ingest/query control ops. Small allow-lists so a caller can't smuggle an
# arbitrary controller path.
ALLOWED_INGEST_OPS: frozenset[str] = frozenset({"pause", "resume"})
ALLOWED_QUERY_OPS: frozenset[str] = frozenset({"allow", "disallow"})


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------


def assert_fts_index_name(name: str) -> None:
    """Reject FTS index names that don't match Couchbase's documented charset.

    Also prevents path traversal / injection since the value is interpolated
    into a REST URL path.
    """
    if not isinstance(name, str) or not _INDEX_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid FTS index name {name!r}. Names must be 1-200 chars "
            "of [A-Za-z0-9._%-]."
        )


def _encode_path_segment(value: str) -> str:
    """URL-encode a value for use as a REST path segment.

    Same helper pattern as ``bucket_admin_rest._encode_path_segment`` and
    ``xdcr_rest._encode_path_segment``. ``%`` is a valid character in FTS
    index names, and raw interpolation could produce path-confusion
    vectors if a name were, e.g., ``%2f%2e%2e``. Encoding with ``safe=""``
    produces the double-encoded form the server treats as an opaque name.
    """
    return quote(value, safe="")


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _fts_base(connection_string: str) -> tuple[str, int]:
    """Return (scheme, port) for the FTS service endpoint."""
    is_tls = connection_string.lower().startswith("couchbases://")
    return ("https", _FTS_TLS_PORT) if is_tls else ("http", _FTS_PORT)


def _request(
    *,
    method: str,
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None,
    timeout: int,
    path: str,
    json_body: Any = None,
    expected_ok: tuple[int, ...] = (200, 201, 202),
) -> dict[str, Any]:
    """Issue a REST request to the FTS service, trying each host in turn.

    FTS endpoints accept and return JSON. On success returns the parsed JSON
    body when present, or an empty dict. Raises ``RuntimeError`` if every
    host fails.
    """
    hosts = _extract_hosts_from_connection_string(connection_string)
    scheme, port = _fts_base(connection_string)
    verify_ssl = _determine_ssl_verification(connection_string, ca_cert_path)

    last_error: Exception | None = None
    with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
        for host in hosts:
            url = f"{scheme}://{host}:{port}{path}"
            try:
                logger.info(f"{method} {url}")
                headers = (
                    {"Content-Type": "application/json"}
                    if json_body is not None
                    else {}
                )
                content = (
                    json.dumps(json_body).encode("utf-8")
                    if json_body is not None
                    else None
                )
                if method == "GET":
                    resp = client.get(url, auth=(username, password))
                elif method == "DELETE":
                    resp = client.delete(url, auth=(username, password))
                elif method == "PUT":
                    resp = client.put(
                        url,
                        content=content,
                        headers=headers,
                        auth=(username, password),
                    )
                elif method == "POST":
                    resp = client.post(
                        url,
                        content=content,
                        headers=headers,
                        auth=(username, password),
                    )
                else:
                    raise ValueError(f"unsupported HTTP method: {method}")
                if resp.status_code not in expected_ok:
                    resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if resp.content and content_type.startswith("application/json"):
                    return resp.json()
                return {}
            except httpx.HTTPError as e:
                logger.warning(f"FTS {method} failed on {host}: {e}")
                last_error = e

    error_msg = f"FTS {method} {path} failed on all hosts: {hosts}"
    if last_error:
        error_msg += f". Last error: {last_error}"
    logger.error(error_msg)
    raise RuntimeError(error_msg)


# --------------------------------------------------------------------------
# Public REST wrappers
# --------------------------------------------------------------------------


def list_fts_indexes_rest(
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET /api/index — list all indexes on the cluster."""
    return _request(
        method="GET",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=_INDEX_PATH,
    )


def get_fts_index_rest(
    connection_string: str,
    username: str,
    password: str,
    index_name: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET /api/index/{name} — return the full index definition."""
    assert_fts_index_name(index_name)
    return _request(
        method="GET",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=f"{_INDEX_PATH}/{_encode_path_segment(index_name)}",
    )


def get_fts_index_count_rest(
    connection_string: str,
    username: str,
    password: str,
    index_name: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET /api/index/{name}/count — return document count in the index."""
    assert_fts_index_name(index_name)
    return _request(
        method="GET",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=f"{_INDEX_PATH}/{_encode_path_segment(index_name)}/count",
    )


def create_or_update_fts_index_rest(
    connection_string: str,
    username: str,
    password: str,
    index_name: str,
    definition: dict[str, Any],
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """PUT /api/index/{name} — create or update an index.

    Couchbase's FTS API uses the same endpoint for create and update; the
    server distinguishes by whether the index already exists. Callers
    signal intent via the tool-layer functions ``create_fts_index`` vs.
    ``update_fts_index``.
    """
    assert_fts_index_name(index_name)
    return _request(
        method="PUT",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=f"{_INDEX_PATH}/{_encode_path_segment(index_name)}",
        json_body=definition,
    )


def delete_fts_index_rest(
    connection_string: str,
    username: str,
    password: str,
    index_name: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """DELETE /api/index/{name}."""
    assert_fts_index_name(index_name)
    return _request(
        method="DELETE",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=f"{_INDEX_PATH}/{_encode_path_segment(index_name)}",
    )


def ingest_control_rest(
    connection_string: str,
    username: str,
    password: str,
    index_name: str,
    op: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST /api/index/{name}/ingestControl/{pause|resume}."""
    assert_fts_index_name(index_name)
    if op not in ALLOWED_INGEST_OPS:
        raise ValueError(f"op must be one of {sorted(ALLOWED_INGEST_OPS)}, got {op!r}")
    return _request(
        method="POST",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=(f"{_INDEX_PATH}/{_encode_path_segment(index_name)}/ingestControl/{op}"),
    )


def query_control_rest(
    connection_string: str,
    username: str,
    password: str,
    index_name: str,
    op: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST /api/index/{name}/queryControl/{allow|disallow}."""
    assert_fts_index_name(index_name)
    if op not in ALLOWED_QUERY_OPS:
        raise ValueError(f"op must be one of {sorted(ALLOWED_QUERY_OPS)}, got {op!r}")
    return _request(
        method="POST",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=(f"{_INDEX_PATH}/{_encode_path_segment(index_name)}/queryControl/{op}"),
    )


def analyze_doc_rest(
    connection_string: str,
    username: str,
    password: str,
    index_name: str,
    doc: dict[str, Any],
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST /api/analyzeDoc/{name} with a JSON document body.

    Returns how the given document would be analyzed against the index's
    mapping (tokenization, byte offsets, field routing). Useful for
    debugging analyzer configuration without indexing real data.
    """
    assert_fts_index_name(index_name)
    return _request(
        method="POST",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=f"{_ANALYZE_DOC_PATH}/{_encode_path_segment(index_name)}",
        json_body=doc,
    )
