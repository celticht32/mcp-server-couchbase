"""
Unit tests for the XDCR admin tool bodies (parameter validation,
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

from cb_mcp.tools.xdcr_admin import (
    _require_write_scope,
    create_remote_cluster,
    create_replication,
    delete_remote_cluster,
    delete_replication,
    get_replication_settings,
    list_remote_clusters,
    list_replications,
    pause_replication,
    resume_replication,
    update_remote_cluster,
    update_replication_settings,
)
from cb_mcp.utils import xdcr_rest
from cb_mcp.utils.constants import SCOPE_WRITE
from cb_mcp.utils.xdcr_rest import (
    _encode_path_segment,
    _encoded_replication_id,
    _form_value,
    assert_remote_cluster_name,
    assert_replication_id,
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
    with patch("cb_mcp.tools.xdcr_admin.get_access_token", return_value=token):
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
    client.delete.return_value = resp

    ctx_mgr = MagicMock()
    ctx_mgr.__enter__.return_value = client
    ctx_mgr.__exit__.return_value = False
    return ctx_mgr, client


CTX = _ctx()

VALID_ID = "abc123/src-bucket/tgt-bucket"


# --------------------------------------------------------------------------
# assert_remote_cluster_name
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["my-remote", "cluster_1", "prod.dc2", "A%B", "x" * 100],
)
def test_remote_cluster_name_valid(name):
    assert_remote_cluster_name(name)


@pytest.mark.parametrize(
    "name",
    ["", "x" * 101, "with/slash", "with space", "with\nnewline", None, 42],
)
def test_remote_cluster_name_invalid(name):
    with pytest.raises(ValueError, match="Invalid remote-cluster name"):
        assert_remote_cluster_name(name)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# assert_replication_id
# --------------------------------------------------------------------------


def test_replication_id_valid():
    assert_replication_id("uuid-1234/src/tgt")


def test_replication_id_valid_with_percent():
    assert_replication_id("uuid-1234/src%bucket/tgt.bucket")


@pytest.mark.parametrize(
    "rid",
    [
        "",
        "no-slashes-at-all",
        "only/two-segments",
        "four/segments/here/toomany",
        "uuid/src/tgt/",
        "/uuid/src/tgt",
        "uuid/src bucket/tgt",  # space in segment
        "uuid/src\nbucket/tgt",  # newline
    ],
)
def test_replication_id_invalid(rid):
    with pytest.raises(ValueError, match="Invalid replication ID"):
        assert_replication_id(rid)


def test_replication_id_rejects_non_string():
    with pytest.raises(ValueError, match="Invalid replication ID"):
        assert_replication_id(None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# _encode_path_segment and _encoded_replication_id
# --------------------------------------------------------------------------


def test_encode_path_segment_percent_encoded():
    assert _encode_path_segment("%2f%2e%2e") == "%252f%252e%252e"


def test_encoded_replication_id_preserves_segment_slashes():
    """Slashes between segments stay; anything inside a segment would be encoded."""
    result = _encoded_replication_id("uuid1/src-bucket/tgt-bucket")
    # The slashes between segments are preserved
    assert result.count("/") == 2
    # Segment content is passed through since it's already URL-safe
    assert result == "uuid1/src-bucket/tgt-bucket"


def test_encoded_replication_id_encodes_percent_within_segment():
    result = _encoded_replication_id("uuid1/src%bucket/tgt")
    # % within a segment gets encoded
    assert result == "uuid1/src%25bucket/tgt"


# --------------------------------------------------------------------------
# _form_value
# --------------------------------------------------------------------------


def test_form_value_bool():
    assert _form_value(True) == "true"
    assert _form_value(False) == "false"


def test_form_value_int():
    assert _form_value(42) == "42"


def test_form_value_dict_json_encoded():
    v = _form_value({"rules": {"src": "tgt"}})
    assert json.loads(v) == {"rules": {"src": "tgt"}}


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
# create_remote_cluster
# --------------------------------------------------------------------------


def test_create_remote_cluster_maps_snake_to_camel(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.xdcr_admin.create_remote_cluster_rest",
        lambda **kw: captured.update(kw) or {},
    )
    with _mock_token(None):
        result = create_remote_cluster(
            CTX,
            name="my-remote",
            hostname="couchbases://target.example.com",
            username="admin",
            password="secret",
            demand_encryption=True,
            encryption_type="full",
        )
    form = captured["form"]
    assert form["name"] == "my-remote"
    assert form["hostname"] == "couchbases://target.example.com"
    assert form["demandEncryption"] is True
    assert form["encryptionType"] == "full"
    assert result["name"] == "my-remote"


def test_create_remote_cluster_rejects_bad_name():
    with _mock_token(None), pytest.raises(ValueError, match="Invalid remote-cluster"):
        create_remote_cluster(
            CTX,
            name="bad/name",
            hostname="couchbase://x",
            username="u",
            password="p",
        )


def test_create_remote_cluster_extra_forwarded(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.xdcr_admin.create_remote_cluster_rest",
        lambda **kw: captured.update(kw) or {},
    )
    with _mock_token(None):
        create_remote_cluster(
            CTX,
            name="r1",
            hostname="couchbase://x",
            username="u",
            password="p",
            extra={"certificate": "-----BEGIN CERT-----..."},
        )
    assert captured["form"]["certificate"].startswith("-----BEGIN CERT")


def test_create_remote_cluster_rejects_unknown_extra():
    with _mock_token(None), pytest.raises(ValueError, match="Unknown XDCR"):
        create_remote_cluster(
            CTX,
            name="r1",
            hostname="couchbase://x",
            username="u",
            password="p",
            extra={"bogusKey": "x"},
        )


# --------------------------------------------------------------------------
# update_remote_cluster
# --------------------------------------------------------------------------


def test_update_remote_cluster_requires_a_field():
    with _mock_token(None), pytest.raises(ValueError, match="at least one"):
        update_remote_cluster(CTX, name="r1")


def test_update_remote_cluster_sends_only_provided_fields(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.xdcr_admin.update_remote_cluster_rest",
        lambda **kw: captured.update(kw) or {},
    )
    with _mock_token(None):
        update_remote_cluster(CTX, name="r1", hostname="couchbase://new-target")
    assert captured["form"] == {"hostname": "couchbase://new-target"}
    assert captured["name"] == "r1"


# --------------------------------------------------------------------------
# delete_remote_cluster
# --------------------------------------------------------------------------


def test_delete_remote_cluster_requires_confirm():
    with _mock_token(None), pytest.raises(ValueError, match="confirm=True"):
        delete_remote_cluster(CTX, name="r1")


def test_delete_remote_cluster_requires_matching_name():
    with _mock_token(None), pytest.raises(ValueError, match="confirm_name"):
        delete_remote_cluster(CTX, name="r1", confirm=True, confirm_name="r2")


def test_delete_remote_cluster_succeeds(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.xdcr_admin.delete_remote_cluster_rest",
        lambda **_: {"ok": True},
    )
    with _mock_token(None):
        result = delete_remote_cluster(CTX, name="r1", confirm=True, confirm_name="r1")
    assert result == {"name": "r1", "deleted": True, "result": {"ok": True}}


# --------------------------------------------------------------------------
# list_remote_clusters
# --------------------------------------------------------------------------


def test_list_remote_clusters(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.xdcr_admin.list_remote_clusters_rest",
        lambda **_: [{"name": "r1"}, {"name": "r2"}],
    )
    result = list_remote_clusters(CTX)
    assert result == {"remote_clusters": [{"name": "r1"}, {"name": "r2"}]}


# --------------------------------------------------------------------------
# create_replication
# --------------------------------------------------------------------------


def test_create_replication_defaults_continuous(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.xdcr_admin.create_replication_rest",
        lambda **kw: captured.update(kw) or {"id": VALID_ID},
    )
    with _mock_token(None):
        result = create_replication(
            CTX, from_bucket="src", to_cluster="my-remote", to_bucket="tgt"
        )
    assert captured["form"]["replicationType"] == "continuous"
    assert result["replication_id"] == VALID_ID


def test_create_replication_rejects_bad_type():
    with _mock_token(None), pytest.raises(ValueError, match="continuous"):
        create_replication(
            CTX,
            from_bucket="src",
            to_cluster="my-remote",
            to_bucket="tgt",
            replication_type="bogus",
        )


def test_create_replication_forwards_optional_settings(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.xdcr_admin.create_replication_rest",
        lambda **kw: captured.update(kw) or {},
    )
    with _mock_token(None):
        create_replication(
            CTX,
            from_bucket="src",
            to_cluster="my-remote",
            to_bucket="tgt",
            priority="High",
            compression_type="Snappy",
        )
    assert captured["form"]["priority"] == "High"
    assert captured["form"]["compressionType"] == "Snappy"


# --------------------------------------------------------------------------
# delete_replication
# --------------------------------------------------------------------------


def test_delete_replication_requires_confirm():
    with _mock_token(None), pytest.raises(ValueError, match="confirm=True"):
        delete_replication(CTX, replication_id=VALID_ID)


def test_delete_replication_requires_matching_name():
    with _mock_token(None), pytest.raises(ValueError, match="confirm_name"):
        delete_replication(
            CTX, replication_id=VALID_ID, confirm=True, confirm_name="wrong"
        )


def test_delete_replication_rejects_bad_id():
    with _mock_token(None), pytest.raises(ValueError, match="Invalid replication ID"):
        delete_replication(CTX, replication_id="not-a-valid-id")


def test_delete_replication_succeeds(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.xdcr_admin.delete_replication_rest",
        lambda **_: {"ok": True},
    )
    with _mock_token(None):
        result = delete_replication(
            CTX, replication_id=VALID_ID, confirm=True, confirm_name=VALID_ID
        )
    assert result["deleted"] is True
    assert result["replication_id"] == VALID_ID


# --------------------------------------------------------------------------
# pause_replication / resume_replication
# --------------------------------------------------------------------------


def test_pause_replication_sends_pause_true(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.xdcr_admin.update_replication_settings_rest",
        lambda **kw: captured.update(kw) or {},
    )
    with _mock_token(None):
        result = pause_replication(CTX, replication_id=VALID_ID)
    assert captured["form"] == {"pauseRequested": True}
    assert result["paused"] is True


def test_resume_replication_sends_pause_false(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.xdcr_admin.update_replication_settings_rest",
        lambda **kw: captured.update(kw) or {},
    )
    with _mock_token(None):
        result = resume_replication(CTX, replication_id=VALID_ID)
    assert captured["form"] == {"pauseRequested": False}
    assert result["resumed"] is True


def test_pause_replication_rejects_bad_id():
    with _mock_token(None), pytest.raises(ValueError, match="Invalid replication ID"):
        pause_replication(CTX, replication_id="badid")


# --------------------------------------------------------------------------
# list_replications
# --------------------------------------------------------------------------


def test_list_replications(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.xdcr_admin.list_replications_rest",
        lambda **_: [{"id": VALID_ID, "type": "xdcr"}],
    )
    result = list_replications(CTX)
    assert result == {"replications": [{"id": VALID_ID, "type": "xdcr"}]}


# --------------------------------------------------------------------------
# get_replication_settings
# --------------------------------------------------------------------------


def test_get_replication_settings(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.xdcr_admin.get_replication_settings_rest",
        lambda **_: {"priority": "High", "pauseRequested": False},
    )
    result = get_replication_settings(CTX, replication_id=VALID_ID)
    assert result == {"priority": "High", "pauseRequested": False}


def test_get_replication_settings_rejects_bad_id():
    with pytest.raises(ValueError, match="Invalid replication ID"):
        get_replication_settings(CTX, replication_id="badid")


# --------------------------------------------------------------------------
# update_replication_settings
# --------------------------------------------------------------------------


def test_update_replication_settings_requires_a_field():
    with _mock_token(None), pytest.raises(ValueError, match="at least one"):
        update_replication_settings(CTX, replication_id=VALID_ID)


def test_update_replication_settings_maps_snake_to_camel(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.xdcr_admin.update_replication_settings_rest",
        lambda **kw: captured.update(kw) or {},
    )
    with _mock_token(None):
        update_replication_settings(
            CTX,
            replication_id=VALID_ID,
            priority="Low",
            checkpoint_interval=600,
            worker_batch_size=500,
            collections_migration_mode=True,
        )
    form = captured["form"]
    assert form["priority"] == "Low"
    assert form["checkpointInterval"] == 600
    assert form["workerBatchSize"] == 500
    assert form["collectionsMigrationMode"] is True


def test_update_replication_settings_dict_field(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.xdcr_admin.update_replication_settings_rest",
        lambda **kw: captured.update(kw) or {},
    )
    with _mock_token(None):
        update_replication_settings(
            CTX,
            replication_id=VALID_ID,
            collections_mapping_rules={"src.scope1": "tgt.scope2"},
        )
    assert captured["form"]["collectionsMappingRules"] == {"src.scope1": "tgt.scope2"}


# --------------------------------------------------------------------------
# REST layer URL construction
# --------------------------------------------------------------------------


def test_delete_remote_cluster_rest_url(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(xdcr_rest.httpx, "Client", lambda **_: ctx_mgr)

    xdcr_rest.delete_remote_cluster_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
        name="r1",
    )
    url = client.delete.call_args.args[0]
    assert url.endswith("/pools/default/remoteClusters/r1")


def test_delete_replication_rest_url(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(xdcr_rest.httpx, "Client", lambda **_: ctx_mgr)

    xdcr_rest.delete_replication_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
        replication_id=VALID_ID,
    )
    url = client.delete.call_args.args[0]
    assert url.endswith(f"/controller/cancelXDCR/{VALID_ID}")


def test_list_replications_rest_filters_to_xdcr(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    resp = client.get.return_value
    resp.json.return_value = [
        {"type": "xdcr", "id": VALID_ID},
        {"type": "rebalance"},
        {"type": "xdcr", "id": "other/src/tgt"},
    ]
    monkeypatch.setattr(xdcr_rest.httpx, "Client", lambda **_: ctx_mgr)

    result = xdcr_rest.list_replications_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
    )
    assert len(result) == 2
    assert all(r["type"] == "xdcr" for r in result)


def test_tls_uses_18091(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(xdcr_rest.httpx, "Client", lambda **_: ctx_mgr)

    xdcr_rest.delete_remote_cluster_rest(
        connection_string="couchbases://mycluster",
        username="u",
        password="p",
        name="r1",
    )
    url = client.delete.call_args.args[0]
    assert url.startswith("https://")
    assert ":18091/" in url
