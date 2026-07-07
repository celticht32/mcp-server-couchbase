"""
Unit tests for the FTS admin tool bodies (parameter validation,
argument construction, confirmation gates, and write-scope gating),
exercised against stubbed REST helpers so no live Couchbase cluster is
required.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.tools.fts_admin import (
    _require_write_scope,
    analyze_document,
    create_fts_index,
    delete_fts_index,
    get_fts_index,
    get_fts_index_count,
    list_fts_indexes,
    pause_fts_index_ingest,
    resume_fts_index_ingest,
    set_fts_index_query_control,
    update_fts_index,
)
from cb_mcp.utils import fts_rest
from cb_mcp.utils.constants import SCOPE_WRITE
from cb_mcp.utils.fts_rest import (
    ALLOWED_INGEST_OPS,
    ALLOWED_QUERY_OPS,
    _encode_path_segment,
    assert_fts_index_name,
)

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


@contextmanager
def _mock_token(scopes):
    class _Tok:
        def __init__(self, scopes_):
            self.scopes = scopes_

    token = _Tok(scopes) if scopes is not None else None
    with patch("cb_mcp.tools.fts_admin.get_access_token", return_value=token):
        yield


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


def _stub_httpx_client():
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b'{"ok":true}'
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = {"ok": True}
    resp.raise_for_status = MagicMock()

    client = MagicMock()
    client.get.return_value = resp
    client.post.return_value = resp
    client.put.return_value = resp
    client.delete.return_value = resp

    ctx_mgr = MagicMock()
    ctx_mgr.__enter__.return_value = client
    ctx_mgr.__exit__.return_value = False
    return ctx_mgr, client


CTX = _ctx()

MIN_DEFINITION = {
    "type": "fulltext-index",
    "name": "will-be-replaced",
    "sourceType": "couchbase",
    "sourceName": "travel-sample",
    "params": {},
}


# --------------------------------------------------------------------------
# assert_fts_index_name
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["idx-1", "hotel_index", "prod.v2", "A%B", "x" * 200],
)
def test_fts_index_name_valid(name):
    assert_fts_index_name(name)


@pytest.mark.parametrize(
    "name",
    ["", "x" * 201, "with/slash", "with space", "with\nnewline", None, 42],
)
def test_fts_index_name_invalid(name):
    with pytest.raises(ValueError, match="Invalid FTS index name"):
        assert_fts_index_name(name)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# _encode_path_segment
# --------------------------------------------------------------------------


def test_encode_path_segment_double_encodes_percent():
    assert _encode_path_segment("%2f%2e%2e") == "%252f%252e%252e"


def test_encode_path_segment_preserves_normal_names():
    assert _encode_path_segment("hotel-index") == "hotel-index"


# --------------------------------------------------------------------------
# _require_write_scope
# --------------------------------------------------------------------------


def test_write_scope_noop_without_token():
    with _mock_token(None):
        _require_write_scope()


def test_write_scope_raises_when_missing():
    with _mock_token(["read"]), pytest.raises(PermissionError, match="write"):
        _require_write_scope()


def test_write_scope_passes_when_present():
    with _mock_token(["read", SCOPE_WRITE]):
        _require_write_scope()


# --------------------------------------------------------------------------
# Read tools
# --------------------------------------------------------------------------


def test_list_fts_indexes(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.fts_admin.list_fts_indexes_rest",
        lambda **_: {"indexDefs": {}, "status": "ok"},
    )
    result = list_fts_indexes(CTX)
    assert result == {"indexDefs": {}, "status": "ok"}


def test_get_fts_index(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.fts_admin.get_fts_index_rest",
        lambda **_: {"indexDef": {"name": "idx-1"}},
    )
    result = get_fts_index(CTX, index_name="idx-1")
    assert result == {"indexDef": {"name": "idx-1"}}


def test_get_fts_index_rejects_bad_name():
    with pytest.raises(ValueError, match="Invalid FTS index name"):
        get_fts_index(CTX, index_name="bad/name")


def test_get_fts_index_count(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.fts_admin.get_fts_index_count_rest",
        lambda **_: {"count": 12345, "status": "ok"},
    )
    result = get_fts_index_count(CTX, index_name="idx-1")
    assert result == {"count": 12345, "status": "ok"}


def test_analyze_document(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.fts_admin.analyze_doc_rest",
        lambda **kw: captured.update(kw) or {"analyzed": []},
    )
    result = analyze_document(
        CTX,
        index_name="idx-1",
        doc={"title": "Hello world"},
    )
    assert captured["doc"] == {"title": "Hello world"}
    assert captured["index_name"] == "idx-1"
    assert result == {"analyzed": []}


def test_analyze_document_rejects_non_dict_doc():
    with pytest.raises(ValueError, match="doc must be a dict"):
        analyze_document(CTX, index_name="idx-1", doc="not a dict")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# create_fts_index
# --------------------------------------------------------------------------


def test_create_fts_index_normalizes_name(monkeypatch):
    """The definition's 'name' field should be forced to match index_name."""
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.fts_admin.create_or_update_fts_index_rest",
        lambda **kw: captured.update(kw) or {},
    )
    with _mock_token(None):
        result = create_fts_index(
            CTX,
            index_name="my-idx",
            definition=dict(MIN_DEFINITION),
        )
    assert captured["definition"]["name"] == "my-idx"
    assert result["index_name"] == "my-idx"


def test_create_fts_index_rejects_non_dict_definition():
    with (
        _mock_token(None),
        pytest.raises(ValueError, match="definition must be a dict"),
    ):
        create_fts_index(CTX, index_name="my-idx", definition="not a dict")  # type: ignore[arg-type]


def test_create_fts_index_rejects_bad_name():
    with _mock_token(None), pytest.raises(ValueError, match="Invalid FTS index name"):
        create_fts_index(CTX, index_name="bad/name", definition=dict(MIN_DEFINITION))


def test_create_fts_index_write_scope_required():
    with _mock_token(["read"]), pytest.raises(PermissionError, match="write"):
        create_fts_index(CTX, index_name="my-idx", definition=dict(MIN_DEFINITION))


# --------------------------------------------------------------------------
# update_fts_index
# --------------------------------------------------------------------------


def test_update_fts_index_uses_same_endpoint(monkeypatch):
    """update_fts_index and create_fts_index both hit create_or_update_fts_index_rest."""
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.fts_admin.create_or_update_fts_index_rest",
        lambda **kw: captured.update(kw) or {},
    )
    with _mock_token(None):
        update_fts_index(
            CTX,
            index_name="my-idx",
            definition=dict(MIN_DEFINITION),
        )
    assert captured["index_name"] == "my-idx"
    assert captured["definition"]["name"] == "my-idx"


# --------------------------------------------------------------------------
# delete_fts_index
# --------------------------------------------------------------------------


def test_delete_fts_index_requires_confirm_true():
    with _mock_token(None), pytest.raises(ValueError, match="confirm=True"):
        delete_fts_index(CTX, index_name="my-idx")


def test_delete_fts_index_requires_matching_confirm_name():
    with _mock_token(None), pytest.raises(ValueError, match="confirm_name"):
        delete_fts_index(CTX, index_name="my-idx", confirm=True, confirm_name="wrong")


def test_delete_fts_index_requires_confirm_name_explicitly():
    with _mock_token(None), pytest.raises(ValueError, match="confirm_name"):
        delete_fts_index(CTX, index_name="my-idx", confirm=True)


def test_delete_fts_index_succeeds(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.fts_admin.delete_fts_index_rest",
        lambda **_: {"status": "ok"},
    )
    with _mock_token(None):
        result = delete_fts_index(
            CTX, index_name="my-idx", confirm=True, confirm_name="my-idx"
        )
    assert result == {
        "index_name": "my-idx",
        "deleted": True,
        "result": {"status": "ok"},
    }


# --------------------------------------------------------------------------
# pause_fts_index_ingest / resume_fts_index_ingest
# --------------------------------------------------------------------------


def test_pause_fts_index_ingest(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.fts_admin.ingest_control_rest",
        lambda **kw: captured.update(kw) or {},
    )
    with _mock_token(None):
        result = pause_fts_index_ingest(CTX, index_name="my-idx")
    assert captured["op"] == "pause"
    assert result["ingest_paused"] is True


def test_resume_fts_index_ingest(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.fts_admin.ingest_control_rest",
        lambda **kw: captured.update(kw) or {},
    )
    with _mock_token(None):
        result = resume_fts_index_ingest(CTX, index_name="my-idx")
    assert captured["op"] == "resume"
    assert result["ingest_resumed"] is True


# --------------------------------------------------------------------------
# set_fts_index_query_control
# --------------------------------------------------------------------------


def test_set_fts_index_query_control_allow(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.fts_admin.query_control_rest",
        lambda **kw: captured.update(kw) or {},
    )
    with _mock_token(None):
        result = set_fts_index_query_control(CTX, index_name="my-idx", allow=True)
    assert captured["op"] == "allow"
    assert result["queries_allowed"] is True


def test_set_fts_index_query_control_disallow(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.fts_admin.query_control_rest",
        lambda **kw: captured.update(kw) or {},
    )
    with _mock_token(None):
        result = set_fts_index_query_control(CTX, index_name="my-idx", allow=False)
    assert captured["op"] == "disallow"
    assert result["queries_allowed"] is False


# --------------------------------------------------------------------------
# Op allow-lists match between tool and REST
# --------------------------------------------------------------------------


def test_ingest_ops_allow_list():
    assert frozenset({"pause", "resume"}) == ALLOWED_INGEST_OPS


def test_query_ops_allow_list():
    assert frozenset({"allow", "disallow"}) == ALLOWED_QUERY_OPS


# --------------------------------------------------------------------------
# REST layer URL construction (httpx stubbed)
# --------------------------------------------------------------------------


def test_get_index_url_and_port(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(fts_rest.httpx, "Client", lambda **_: ctx_mgr)

    fts_rest.get_fts_index_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
        index_name="idx-1",
    )
    url = client.get.call_args.args[0]
    assert url.endswith("/api/index/idx-1")
    assert ":8094/" in url


def test_tls_uses_18094(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(fts_rest.httpx, "Client", lambda **_: ctx_mgr)

    fts_rest.get_fts_index_rest(
        connection_string="couchbases://mycluster",
        username="u",
        password="p",
        index_name="idx-1",
    )
    url = client.get.call_args.args[0]
    assert url.startswith("https://")
    assert ":18094/" in url


def test_ingest_control_rest_url(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(fts_rest.httpx, "Client", lambda **_: ctx_mgr)

    fts_rest.ingest_control_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
        index_name="idx-1",
        op="pause",
    )
    url = client.post.call_args.args[0]
    assert url.endswith("/api/index/idx-1/ingestControl/pause")


def test_ingest_control_rest_rejects_bad_op():
    with pytest.raises(ValueError, match="op must be"):
        fts_rest.ingest_control_rest(
            connection_string="couchbase://localhost",
            username="u",
            password="p",
            index_name="idx-1",
            op="stop",
        )


def test_query_control_rest_url(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(fts_rest.httpx, "Client", lambda **_: ctx_mgr)

    fts_rest.query_control_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
        index_name="idx-1",
        op="disallow",
    )
    url = client.post.call_args.args[0]
    assert url.endswith("/api/index/idx-1/queryControl/disallow")


def test_analyze_doc_rest_url_and_body(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(fts_rest.httpx, "Client", lambda **_: ctx_mgr)

    fts_rest.analyze_doc_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
        index_name="idx-1",
        doc={"title": "Hello"},
    )
    call_kwargs = client.post.call_args.kwargs
    assert client.post.call_args.args[0].endswith("/api/analyzeDoc/idx-1")
    assert json.loads(call_kwargs["content"]) == {"title": "Hello"}
    assert call_kwargs["headers"]["Content-Type"] == "application/json"


def test_create_index_uses_put(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(fts_rest.httpx, "Client", lambda **_: ctx_mgr)

    fts_rest.create_or_update_fts_index_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
        index_name="idx-1",
        definition={"type": "fulltext-index", "name": "idx-1"},
    )
    # Verify PUT was called, not POST
    client.put.assert_called_once()
    url = client.put.call_args.args[0]
    assert url.endswith("/api/index/idx-1")
