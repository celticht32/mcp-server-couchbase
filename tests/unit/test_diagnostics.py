"""
Unit tests for the diagnostics tool bodies (URL construction, subset
extraction, and slow-query SQL construction).

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cb_mcp.tools.diagnostics import (
    get_bucket_stats,
    get_cluster_info,
    get_cluster_stats,
    get_disk_usage,
    get_error_logs,
    get_fts_stats,
    get_index_stats,
    get_kv_stats,
    get_node_info,
    get_query_stats,
    get_slow_queries,
)
from cb_mcp.utils import diagnostics_rest

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


def _ctx():
    settings = {
        "connection_string": "couchbase://localhost",
        "username": "Administrator",
        "password": "password",
        "ca_cert_path": None,
    }
    lifespan = SimpleNamespace(settings=settings)
    request = SimpleNamespace(lifespan_context=lifespan)
    return SimpleNamespace(request_context=request)


def _stub_httpx_client(json_body=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b'{"ok":true}'
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = json_body if json_body is not None else {"ok": True}
    resp.raise_for_status = MagicMock()

    client = MagicMock()
    client.get.return_value = resp

    ctx_mgr = MagicMock()
    ctx_mgr.__enter__.return_value = client
    ctx_mgr.__exit__.return_value = False
    return ctx_mgr, client


CTX = _ctx()


# --------------------------------------------------------------------------
# get_cluster_info / get_cluster_stats
# --------------------------------------------------------------------------


def test_get_cluster_info_returns_raw_response(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.diagnostics.get_pools_default_rest",
        lambda **_: {"nodes": [], "storageTotals": {}, "balanced": True},
    )
    result = get_cluster_info(CTX)
    assert result == {"nodes": [], "storageTotals": {}, "balanced": True}


def test_get_cluster_stats_synthesizes_summary(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.diagnostics.get_pools_default_rest",
        lambda **_: {
            "nodes": [
                {"status": "healthy"},
                {"status": "healthy"},
                {"status": "warmup"},
            ],
            "storageTotals": {"ram": {"total": 100}},
            "balanced": True,
            "rebalanceStatus": "none",
        },
    )
    result = get_cluster_stats(CTX)
    assert result["node_count"] == 3
    assert result["healthy_nodes"] == 2
    assert result["storage_totals"] == {"ram": {"total": 100}}
    assert result["balanced"] is True
    assert result["rebalance_status"] == "none"


def test_get_cluster_stats_handles_empty_response(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.diagnostics.get_pools_default_rest",
        lambda **_: {},
    )
    result = get_cluster_stats(CTX)
    assert result["node_count"] == 0
    assert result["healthy_nodes"] == 0


# --------------------------------------------------------------------------
# get_node_info
# --------------------------------------------------------------------------


def test_get_node_info(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.diagnostics.get_node_self_rest",
        lambda **_: {"hostname": "node1:8091", "uptime": 12345},
    )
    result = get_node_info(CTX)
    assert result == {"hostname": "node1:8091", "uptime": 12345}


# --------------------------------------------------------------------------
# get_bucket_stats
# --------------------------------------------------------------------------


def test_get_bucket_stats(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.diagnostics.get_bucket_stats_rest",
        lambda **kw: captured.update(kw) or {"op": {"samples": []}},
    )
    result = get_bucket_stats(CTX, bucket_name="travel-sample")
    assert captured["bucket_name"] == "travel-sample"
    assert result == {"op": {"samples": []}}


# --------------------------------------------------------------------------
# get_index_stats / get_query_stats / get_fts_stats
# --------------------------------------------------------------------------


def test_get_index_stats(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.diagnostics.get_index_stats_rest",
        lambda **_: {"indexer_ram_used": 12345},
    )
    result = get_index_stats(CTX)
    assert result == {"indexer_ram_used": 12345}


def test_get_query_stats(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.diagnostics.get_query_stats_rest",
        lambda **_: {"active_requests": 3},
    )
    result = get_query_stats(CTX)
    assert result == {"active_requests": 3}


def test_get_fts_stats(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.diagnostics.get_fts_stats_rest",
        lambda **_: {"num_indexes": 2},
    )
    result = get_fts_stats(CTX)
    assert result == {"num_indexes": 2}


# --------------------------------------------------------------------------
# get_kv_stats
# --------------------------------------------------------------------------


def test_get_kv_stats(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.diagnostics.get_kv_stats_rest",
        lambda **kw: captured.update(kw) or {"curr_items": 12345},
    )
    result = get_kv_stats(CTX, bucket_name="travel-sample")
    assert captured["bucket_name"] == "travel-sample"
    assert result == {"curr_items": 12345}


# --------------------------------------------------------------------------
# get_disk_usage
# --------------------------------------------------------------------------


def test_get_disk_usage_extracts_storage_totals(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.diagnostics.get_pools_default_rest",
        lambda **_: {
            "storageTotals": {
                "ram": {"total": 1000, "used": 500},
                "hdd": {"total": 10000, "usedByData": 2000},
            }
        },
    )
    result = get_disk_usage(CTX)
    assert result == {
        "ram": {"total": 1000, "used": 500},
        "hdd": {"total": 10000, "usedByData": 2000},
    }


def test_get_disk_usage_handles_empty(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.diagnostics.get_pools_default_rest",
        lambda **_: {},
    )
    result = get_disk_usage(CTX)
    assert result == {"ram": {}, "hdd": {}}


# --------------------------------------------------------------------------
# get_slow_queries
# --------------------------------------------------------------------------


def test_get_slow_queries_constructs_expected_statement(monkeypatch):
    captured = {}

    def _run(ctx, stmt, **_kw):
        captured["stmt"] = stmt
        return [{"requestId": "abc", "elapsedTime": "1.5s"}]

    monkeypatch.setattr("cb_mcp.tools.diagnostics.run_cluster_query", _run)
    result = get_slow_queries(CTX, min_duration_ms=500, limit=10)
    assert "FROM system:completed_requests" in captured["stmt"]
    assert "STR_TO_DURATION(elapsedTime)/1000000 >= 500" in captured["stmt"]
    assert "LIMIT 10" in captured["stmt"]
    assert result["min_duration_ms"] == 500
    assert result["count"] == 1


def test_get_slow_queries_defaults():
    """Default min_duration=1000ms, limit=50."""
    sig = inspect.signature(get_slow_queries)
    assert sig.parameters["min_duration_ms"].default == 1000
    assert sig.parameters["limit"].default == 50


def test_get_slow_queries_rejects_negative_duration():
    with pytest.raises(ValueError, match="cannot be negative"):
        get_slow_queries(CTX, min_duration_ms=-100)


def test_get_slow_queries_rejects_bad_limit():
    with pytest.raises(ValueError, match="limit must be"):
        get_slow_queries(CTX, limit=0)
    with pytest.raises(ValueError, match="limit must be"):
        get_slow_queries(CTX, limit=1001)


# --------------------------------------------------------------------------
# get_error_logs
# --------------------------------------------------------------------------


def test_get_error_logs(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.diagnostics.get_error_logs_rest",
        lambda **_: {"list": [{"text": "startup ok"}]},
    )
    result = get_error_logs(CTX)
    assert result == {"list": [{"text": "startup ok"}]}


# --------------------------------------------------------------------------
# REST layer URL construction
# --------------------------------------------------------------------------


def test_pools_default_url(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(diagnostics_rest.httpx, "Client", lambda **_: ctx_mgr)

    diagnostics_rest.get_pools_default_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
    )
    url = client.get.call_args.args[0]
    assert url.endswith("/pools/default")
    assert ":8091/" in url


def test_pools_default_tls_uses_18091(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(diagnostics_rest.httpx, "Client", lambda **_: ctx_mgr)

    diagnostics_rest.get_pools_default_rest(
        connection_string="couchbases://mycluster",
        username="u",
        password="p",
    )
    url = client.get.call_args.args[0]
    assert url.startswith("https://")
    assert ":18091/" in url


def test_node_self_url(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(diagnostics_rest.httpx, "Client", lambda **_: ctx_mgr)

    diagnostics_rest.get_node_self_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
    )
    url = client.get.call_args.args[0]
    assert url.endswith("/nodes/self")


def test_bucket_stats_url_uses_encoding(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(diagnostics_rest.httpx, "Client", lambda **_: ctx_mgr)

    diagnostics_rest.get_bucket_stats_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
        bucket_name="travel-sample",
    )
    url = client.get.call_args.args[0]
    assert url.endswith("/pools/default/buckets/travel-sample/stats")


def test_bucket_stats_rejects_bad_bucket_name():
    with pytest.raises(ValueError, match="Invalid bucket name"):
        diagnostics_rest.get_bucket_stats_rest(
            connection_string="couchbase://localhost",
            username="u",
            password="p",
            bucket_name="bad/name",
        )


def test_index_stats_url(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(diagnostics_rest.httpx, "Client", lambda **_: ctx_mgr)

    diagnostics_rest.get_index_stats_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
    )
    url = client.get.call_args.args[0]
    assert url.endswith("/pools/default/buckets/@index/stats")


def test_query_stats_url(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(diagnostics_rest.httpx, "Client", lambda **_: ctx_mgr)

    diagnostics_rest.get_query_stats_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
    )
    url = client.get.call_args.args[0]
    assert url.endswith("/pools/default/buckets/@query/stats")


def test_fts_stats_uses_fts_port_8094(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(diagnostics_rest.httpx, "Client", lambda **_: ctx_mgr)

    diagnostics_rest.get_fts_stats_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
    )
    url = client.get.call_args.args[0]
    assert url.endswith("/api/nsstats")
    assert ":8094/" in url


def test_fts_stats_tls_uses_18094(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(diagnostics_rest.httpx, "Client", lambda **_: ctx_mgr)

    diagnostics_rest.get_fts_stats_rest(
        connection_string="couchbases://mycluster",
        username="u",
        password="p",
    )
    url = client.get.call_args.args[0]
    assert url.startswith("https://")
    assert ":18094/" in url


def test_logs_url(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(diagnostics_rest.httpx, "Client", lambda **_: ctx_mgr)

    diagnostics_rest.get_error_logs_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
    )
    url = client.get.call_args.args[0]
    assert url.endswith("/logs")
