"""
Tools for cluster diagnostics and stats.

Eleven read-only tools that surface the same information available in the
Couchbase cluster manager Web Console but structured for programmatic use
by an MCP client. Every tool is a GET; nothing here mutates state.

Category split
--------------
Nine tools are ordinary read-only (``READ_ONLY_TOOLS``):

- ``get_cluster_info`` — /pools/default (nodes, quotas, storage totals)
- ``get_cluster_stats`` — a subset of /pools/default focused on
  operational stats
- ``get_node_info`` — /nodes/self
- ``get_bucket_stats`` — per-bucket stats
- ``get_index_stats`` — index service stats
- ``get_query_stats`` — query service stats
- ``get_fts_stats`` — FTS service stats
- ``get_kv_stats`` — KV curr_items and related
- ``get_disk_usage`` — synthesized from /pools/default

Two tools land in a new ``SENSITIVE_READ_TOOLS`` category loaded only when
``admin_write_mode`` is on:

- ``get_slow_queries`` — reads ``system:completed_requests`` which can
  contain full query text including data values
- ``get_error_logs`` — recent server log entries which can contain
  connection strings, hostnames, and internal state

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

import logging
from typing import Any

from fastmcp import Context

from ..utils.config import get_settings
from ..utils.constants import MCP_SERVER_NAME
from ..utils.diagnostics_rest import (
    get_bucket_stats_rest,
    get_error_logs_rest,
    get_fts_stats_rest,
    get_index_stats_rest,
    get_kv_stats_rest,
    get_node_self_rest,
    get_pools_default_rest,
    get_query_stats_rest,
)
from .query import run_cluster_query

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.diagnostics")


# --------------------------------------------------------------------------
# READ_ONLY tools
# --------------------------------------------------------------------------


def get_cluster_info(ctx: Context) -> dict[str, Any]:
    """Get cluster-wide info: node list, quotas, storage totals, running tasks.

    Returns the raw ``/pools/default`` response, which contains everything
    the Web Console shows on its Overview screen.
    """
    settings = get_settings(ctx)
    return get_pools_default_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        ca_cert_path=settings.get("ca_cert_path"),
    )


def get_cluster_stats(ctx: Context) -> dict[str, Any]:
    """Get operational stats from the cluster: nodes count, RAM/disk usage.

    A curated subset of ``get_cluster_info`` focused on operational-stats
    fields (rather than the full node list and quota configuration).
    Useful when a caller wants a compact overview without parsing the
    full /pools/default response.
    """
    settings = get_settings(ctx)
    raw = get_pools_default_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        ca_cert_path=settings.get("ca_cert_path"),
    )
    # Pull the interesting subset. Kept as a dict of the fields that
    # actually vary at runtime, so a caller can diff them across polls.
    nodes = raw.get("nodes", []) if isinstance(raw, dict) else []
    storage_totals = raw.get("storageTotals", {}) if isinstance(raw, dict) else {}
    return {
        "node_count": len(nodes),
        "healthy_nodes": sum(
            1 for n in nodes if isinstance(n, dict) and n.get("status") == "healthy"
        ),
        "storage_totals": storage_totals,
        "balanced": raw.get("balanced") if isinstance(raw, dict) else None,
        "rebalance_status": raw.get("rebalanceStatus")
        if isinstance(raw, dict)
        else None,
    }


def get_node_info(ctx: Context) -> dict[str, Any]:
    """Get info for the current node (/nodes/self).

    Returns hostname, uptime, memory used/total, services running, and
    OS-level counters for the node the connection lands on.
    """
    settings = get_settings(ctx)
    return get_node_self_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        ca_cert_path=settings.get("ca_cert_path"),
    )


def get_bucket_stats(ctx: Context, bucket_name: str) -> dict[str, Any]:
    """Get per-bucket runtime stats.

    Returns ops per second, disk queues, memory usage, and cache hit ratio
    for a single bucket. Ranges from a few seconds to a minute of history
    depending on cluster load.
    """
    settings = get_settings(ctx)
    return get_bucket_stats_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        bucket_name=bucket_name,
        ca_cert_path=settings.get("ca_cert_path"),
    )


def get_index_stats(ctx: Context) -> dict[str, Any]:
    """Get Index Service runtime stats.

    Returns index count, memory used by the indexer, average scan latency,
    and per-index timings across the cluster.
    """
    settings = get_settings(ctx)
    return get_index_stats_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        ca_cert_path=settings.get("ca_cert_path"),
    )


def get_query_stats(ctx: Context) -> dict[str, Any]:
    """Get Query Service runtime stats.

    Returns request rate, active request count, and average latency for
    the query engine.
    """
    settings = get_settings(ctx)
    return get_query_stats_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        ca_cert_path=settings.get("ca_cert_path"),
    )


def get_fts_stats(ctx: Context) -> dict[str, Any]:
    """Get FTS (Full-Text Search) service runtime stats.

    Reads /api/nsstats on the FTS service port (8094/18094), not the
    cluster manager. Returns per-index doc counts, query throughput, and
    indexing pipeline stats.
    """
    settings = get_settings(ctx)
    return get_fts_stats_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        ca_cert_path=settings.get("ca_cert_path"),
    )


def get_kv_stats(ctx: Context, bucket_name: str) -> dict[str, Any]:
    """Get KV (data) service stats for a bucket.

    Returns current items count and related KV runtime stats. Requires a
    bucket name because Couchbase KV stats are bucket-scoped.
    """
    settings = get_settings(ctx)
    return get_kv_stats_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        bucket_name=bucket_name,
        ca_cert_path=settings.get("ca_cert_path"),
    )


def get_disk_usage(ctx: Context) -> dict[str, Any]:
    """Get cluster disk usage totals.

    Extracted from ``/pools/default``'s ``storageTotals`` block. Returns
    RAM totals plus HDD usage/quota/free per node aggregated at the
    cluster level.
    """
    settings = get_settings(ctx)
    raw = get_pools_default_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        ca_cert_path=settings.get("ca_cert_path"),
    )
    storage = raw.get("storageTotals", {}) if isinstance(raw, dict) else {}
    return {
        "ram": storage.get("ram", {}),
        "hdd": storage.get("hdd", {}),
    }


# --------------------------------------------------------------------------
# SENSITIVE_READ tools (loaded only when admin_write_mode=True)
# --------------------------------------------------------------------------


def get_slow_queries(
    ctx: Context,
    min_duration_ms: int = 1000,
    limit: int = 50,
) -> dict[str, Any]:
    """Read the ``system:completed_requests`` keyspace for slow queries.

    Returns queries whose ``elapsedTime`` exceeded ``min_duration_ms``,
    ordered by duration descending, capped at ``limit`` rows.

    Note: this reads real query text which may include literal data
    values from prior WHERE clauses. That's why it's in
    ``SENSITIVE_READ_TOOLS`` — the results can contain user data even
    though the tool itself is read-only.
    """
    if min_duration_ms < 0:
        raise ValueError(f"min_duration_ms cannot be negative, got {min_duration_ms}")
    if limit < 1 or limit > 1000:
        raise ValueError(f"limit must be 1-1000, got {limit}")

    # completed_requests stores durations as strings like "1.234s"; the
    # numeric form is available via STR_TO_DURATION_MS
    stmt = (
        "SELECT requestId, statement, elapsedTime, resultCount, "
        "requestTime, users "
        "FROM system:completed_requests "
        f"WHERE STR_TO_DURATION(elapsedTime)/1000000 >= {int(min_duration_ms)} "
        "ORDER BY elapsedTime DESC "
        f"LIMIT {int(limit)}"
    )
    logger.info(f"Reading slow queries (min={min_duration_ms}ms, limit={limit})")
    rows = run_cluster_query(ctx, stmt)
    return {
        "min_duration_ms": min_duration_ms,
        "limit": limit,
        "count": len(rows) if isinstance(rows, list) else 0,
        "slow_queries": rows,
    }


def get_error_logs(ctx: Context) -> dict[str, Any]:
    """Get recent server log entries from the cluster manager.

    Returns the same view the Web Console's Logs tab shows. Log entries
    can contain connection strings, hostnames, and internal state, which
    is why this tool is in ``SENSITIVE_READ_TOOLS``.
    """
    settings = get_settings(ctx)
    return get_error_logs_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        ca_cert_path=settings.get("ca_cert_path"),
    )
