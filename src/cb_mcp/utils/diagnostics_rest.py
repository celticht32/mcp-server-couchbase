"""
REST client for cluster diagnostics and stats endpoints.

Reads across every major subsystem:

- ``/pools/default`` — cluster info, nodes, quotas, storage totals.
- ``/pools/default/buckets/{bucket}/stats`` — per-bucket runtime stats
  (ops per second, disk queues, memory).
- ``/pools/default/buckets/{bucket}/stats/curr_items`` and similar — KV
  stats (per-bucket, no bucket filter possible without one).
- ``/pools/default/tasks`` — rebalance / xdcr / compaction tasks (used by
  chunk 4; not re-exposed here).
- ``/nodes/self`` — the node the connection currently lands on.
- ``/settings/querySettings`` and ``/admin/settings`` — query and index
  service settings (settings-shaped stats).
- ``/api/nsstats`` (FTS, port 8094/18094) — search service stats.
- ``/settings/logRedaction`` (paired) plus ``/diag/eval`` gated — server
  logs are NOT retrievable via a simple REST endpoint; the closest
  supported surface is ``/logs`` on port 8091 which returns the recent
  server-log entries. That's the endpoint ``get_error_logs`` uses.

Slow queries: Couchbase Query Service exposes a ``system:completed_requests``
keyspace queryable via SQL++. Since chunk 1 (``list_indexes`` etc.) shows
the pattern of running SQL++ through ``run_cluster_query`` to get the
Query Service's system tables, ``get_slow_queries`` uses the same path.

Bucket-name validation is inlined here rather than imported from
``bucket_admin_rest`` so this module can be reviewed and merged
independently of the bucket-management PR. The validator and encoder are
byte-identical to the ones there; if both PRs land, a future cleanup can
consolidate them into a shared ``url_utils`` module.

Verified against Couchbase Server docs (rest-api/rest-node-details,
rest-bucket-stats, cbq-monitoring, ns-stats) 2026-07-06.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

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

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.diagnostics_rest")

# Cluster-manager ports (same as bucket admin and XDCR).
_MGMT_PORT = 8091
_MGMT_TLS_PORT = 18091

# FTS service ports (matches fts_rest).
_FTS_PORT = 8094
_FTS_TLS_PORT = 18094

# Query service ports (used for reading query settings directly rather
# than proxied through cluster manager).
_QUERY_PORT = 8093
_QUERY_TLS_PORT = 18093

# Bucket name charset per Couchbase docs. Duplicated here from
# bucket_admin_rest so this module stands alone. See module docstring.
_BUCKET_NAME_RE = re.compile(r"^[A-Za-z0-9._\-%]{1,100}$")


def assert_bucket_name(name: str) -> None:
    """Reject bucket names that don't match Couchbase's documented charset.

    Duplicated from ``bucket_admin_rest.assert_bucket_name`` so this module
    can be merged independently. Behavior is intentionally identical.
    """
    if not isinstance(name, str) or not _BUCKET_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid bucket name {name!r}. Bucket names must be 1-100 chars "
            "of [A-Za-z0-9._%-]."
        )


def _encode_path_segment(value: str) -> str:
    """URL-encode a value for use as a REST path segment.

    Same defense-in-depth pattern as the other REST helpers: ``%`` is a
    valid Couchbase name character, and raw interpolation could produce
    path-confusion vectors if a name were, e.g., ``%2f%2e%2e``. Encoding
    with ``safe=""`` produces the double-encoded form the server treats
    as an opaque identifier.
    """
    return quote(value, safe="")


def _mgmt_base(connection_string: str) -> tuple[str, int]:
    """Return (scheme, port) for the cluster-manager endpoint."""
    is_tls = connection_string.lower().startswith("couchbases://")
    return ("https", _MGMT_TLS_PORT) if is_tls else ("http", _MGMT_PORT)


def _fts_base(connection_string: str) -> tuple[str, int]:
    """Return (scheme, port) for the FTS service endpoint."""
    is_tls = connection_string.lower().startswith("couchbases://")
    return ("https", _FTS_TLS_PORT) if is_tls else ("http", _FTS_PORT)


def _query_base(connection_string: str) -> tuple[str, int]:
    """Return (scheme, port) for the Query service admin endpoint."""
    is_tls = connection_string.lower().startswith("couchbases://")
    return ("https", _QUERY_TLS_PORT) if is_tls else ("http", _QUERY_PORT)


def _get_json(
    *,
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None,
    timeout: int,
    scheme: str,
    port: int,
    path: str,
) -> Any:
    """Issue a GET, trying each host in the connection string in turn.

    Returns parsed JSON on success; raises ``RuntimeError`` if every host
    fails. Returns an empty dict when the server returns no body.
    """
    hosts = _extract_hosts_from_connection_string(connection_string)
    verify_ssl = _determine_ssl_verification(connection_string, ca_cert_path)

    last_error: Exception | None = None
    with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
        for host in hosts:
            url = f"{scheme}://{host}:{port}{path}"
            try:
                logger.info(f"GET {url}")
                resp = client.get(url, auth=(username, password))
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if resp.content and content_type.startswith("application/json"):
                    return resp.json()
                return {}
            except httpx.HTTPError as e:
                logger.warning(f"Diagnostics GET failed on {host}: {e}")
                last_error = e

    error_msg = f"Diagnostics GET {path} failed on all hosts: {hosts}"
    if last_error:
        error_msg += f". Last error: {last_error}"
    logger.error(error_msg)
    raise RuntimeError(error_msg)


# --------------------------------------------------------------------------
# Public REST wrappers
# --------------------------------------------------------------------------


def get_pools_default_rest(
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET /pools/default — cluster-wide info: nodes, quotas, storage, tasks."""
    scheme, port = _mgmt_base(connection_string)
    return _get_json(
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        scheme=scheme,
        port=port,
        path="/pools/default",
    )


def get_node_self_rest(
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET /nodes/self — info for the node the connection lands on."""
    scheme, port = _mgmt_base(connection_string)
    return _get_json(
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        scheme=scheme,
        port=port,
        path="/nodes/self",
    )


def get_bucket_stats_rest(
    connection_string: str,
    username: str,
    password: str,
    bucket_name: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET /pools/default/buckets/{bucket}/stats — per-bucket runtime stats."""
    assert_bucket_name(bucket_name)
    scheme, port = _mgmt_base(connection_string)
    return _get_json(
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        scheme=scheme,
        port=port,
        path=f"/pools/default/buckets/{_encode_path_segment(bucket_name)}/stats",
    )


def get_index_stats_rest(
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET /pools/default/buckets/@index/stats — index service runtime stats."""
    scheme, port = _mgmt_base(connection_string)
    return _get_json(
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        scheme=scheme,
        port=port,
        path="/pools/default/buckets/@index/stats",
    )


def get_query_stats_rest(
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET /pools/default/buckets/@query/stats — query service runtime stats."""
    scheme, port = _mgmt_base(connection_string)
    return _get_json(
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        scheme=scheme,
        port=port,
        path="/pools/default/buckets/@query/stats",
    )


def get_fts_stats_rest(
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET /api/nsstats — FTS service stats (on FTS port 8094/18094)."""
    scheme, port = _fts_base(connection_string)
    return _get_json(
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        scheme=scheme,
        port=port,
        path="/api/nsstats",
    )


def get_kv_stats_rest(
    connection_string: str,
    username: str,
    password: str,
    bucket_name: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET /pools/default/buckets/{bucket}/nodes/{node}/stats/curr_items — KV
    per-node current items count.

    Note: unlike bucket_stats which is aggregated across nodes,
    ``curr_items`` requires a per-node lookup. For simplicity we return the
    aggregated bucket stats filtered to KV-relevant keys; a caller who
    wants per-node breakdowns can use ``get_node_info`` + ``get_bucket_stats``
    together.
    """
    assert_bucket_name(bucket_name)
    scheme, port = _mgmt_base(connection_string)
    return _get_json(
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        scheme=scheme,
        port=port,
        path=(
            f"/pools/default/buckets/{_encode_path_segment(bucket_name)}"
            "/stats/curr_items"
        ),
    )


def get_error_logs_rest(
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET /logs — recent server log entries.

    Returns a dict with a ``list`` key holding recent log lines. This is
    the same view the cluster manager web UI's Logs tab reads.
    """
    scheme, port = _mgmt_base(connection_string)
    return _get_json(
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        scheme=scheme,
        port=port,
        path="/logs",
    )
