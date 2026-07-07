"""
Unit tests for the bucket-management tool bodies (parameter validation,
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

from cb_mcp.tools.bucket_admin import (
    _require_write_scope,
    compact_bucket,
    create_bucket,
    delete_bucket,
    flush_bucket,
    load_sample_bucket,
    update_bucket,
)
from cb_mcp.utils import bucket_admin_rest
from cb_mcp.utils.bucket_admin_rest import (
    ALLOWED_COMPACT_ACTIONS,
    ALLOWED_SAMPLE_BUCKETS,
    _encode_path_segment,
    _form_value,
    assert_bucket_name,
    validate_extra_bucket_keys,
)
from cb_mcp.utils.constants import SCOPE_WRITE

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


@contextmanager
def _mock_token(scopes):
    """Patch get_access_token at the bucket_admin call site.

    Pass a list of scopes for an authenticated token, or None for no token
    (stdio / OAuth disabled).
    """

    class _Tok:
        def __init__(self, scopes_):
            self.scopes = scopes_

    token = _Tok(scopes) if scopes is not None else None
    with patch("cb_mcp.tools.bucket_admin.get_access_token", return_value=token):
        yield


def _ctx(**overrides):
    """Build a Context-shaped SimpleNamespace with default settings."""
    settings = {
        "connection_string": "couchbase://localhost",
        "username": "Administrator",
        "password": "password",
        "ca_cert_path": None,
    }
    settings.update(overrides)
    lifespan = SimpleNamespace(settings=settings)
    request = SimpleNamespace(lifespan_context=lifespan)
    return SimpleNamespace(request_context=request)


def _stub_httpx_client():
    """Return an httpx.Client stub that always returns 200 OK JSON."""
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


# --------------------------------------------------------------------------
# assert_bucket_name
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["travel-sample", "my_bucket", "b1", "A.B_C-D%E", "b" * 100],
)
def test_assert_bucket_name_accepts_valid(name):
    assert_bucket_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "x" * 101,
        "bucket/../etc",
        "bucket/name",
        "bucket\\name",
        "bucket name",
    ],
)
def test_assert_bucket_name_rejects_invalid(name):
    with pytest.raises(ValueError, match="Invalid bucket name"):
        assert_bucket_name(name)


def test_assert_bucket_name_rejects_non_string():
    with pytest.raises(ValueError, match="Invalid bucket name"):
        assert_bucket_name(None)
    with pytest.raises(ValueError, match="Invalid bucket name"):
        assert_bucket_name(123)


# --------------------------------------------------------------------------
# validate_extra_bucket_keys
# --------------------------------------------------------------------------


def test_validate_extra_bucket_keys_accepts_known():
    allowed = frozenset({"ramQuota", "replicaNumber"})
    validate_extra_bucket_keys({"ramQuota": 256, "replicaNumber": 1}, allowed)


def test_validate_extra_bucket_keys_rejects_unknown():
    allowed = frozenset({"ramQuota"})
    with pytest.raises(ValueError, match=r"Unknown bucket setting key"):
        validate_extra_bucket_keys({"ramQuota": 256, "bogus": 1}, allowed)


def test_validate_extra_bucket_keys_accepts_empty_extra():
    validate_extra_bucket_keys({}, frozenset({"ramQuota"}))


# --------------------------------------------------------------------------
# _form_value
# --------------------------------------------------------------------------


def test_form_value_bool_lowercased():
    assert _form_value(True) == "true"
    assert _form_value(False) == "false"


def test_form_value_int():
    assert _form_value(256) == "256"
    assert _form_value(0) == "0"


def test_form_value_str():
    assert _form_value("magma") == "magma"


def test_form_value_dict_json_encoded():
    v = _form_value({"parallelDBAndViewCompaction": True})
    assert json.loads(v) == {"parallelDBAndViewCompaction": True}


# --------------------------------------------------------------------------
# _encode_path_segment (URL path safety for bucket names containing %)
# --------------------------------------------------------------------------


def test_encode_path_segment_percent_encoded_slashes():
    """A bucket name of '%2f%2e%2e' (URL-encoded '/../') is legal per
    Couchbase docs (% is a permitted character), but must be double-encoded
    when placed in a URL path so the server can't route on the decoded form.
    """
    assert _encode_path_segment("%2f%2e%2e") == "%252f%252e%252e"


def test_encode_path_segment_normal_names_unchanged():
    for name in ("travel-sample", "b1", "my_bucket", "with.dots"):
        assert _encode_path_segment(name) == name


# --------------------------------------------------------------------------
# _require_write_scope
# --------------------------------------------------------------------------


def test_require_write_scope_noop_without_token():
    with _mock_token(None):
        _require_write_scope()


def test_require_write_scope_raises_when_missing():
    with _mock_token(["read"]), pytest.raises(PermissionError, match="write"):
        _require_write_scope()


def test_require_write_scope_passes_when_present():
    with _mock_token(["read", SCOPE_WRITE]):
        _require_write_scope()


# --------------------------------------------------------------------------
# create_bucket
# --------------------------------------------------------------------------


def test_create_bucket_requires_name_or_body():
    with _mock_token(None), pytest.raises(ValueError, match="bucket_name is required"):
        create_bucket(CTX, ram_quota_mb=256)


def test_create_bucket_requires_ram_quota_when_structured():
    with _mock_token(None), pytest.raises(ValueError, match="ram_quota_mb is required"):
        create_bucket(CTX, bucket_name="b1")


def test_create_bucket_body_name_mismatch_rejected():
    with _mock_token(None), pytest.raises(ValueError, match="does not match"):
        create_bucket(
            CTX,
            bucket_name="b1",
            body={"name": "b2", "ramQuota": 256},
        )


def test_create_bucket_body_invalid_key_rejected():
    with _mock_token(None), pytest.raises(ValueError, match="Unknown bucket setting"):
        create_bucket(
            CTX,
            body={"name": "b1", "ramQuota": 256, "bogusKey": "x"},
        )


def test_create_bucket_maps_snake_to_camel(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.bucket_admin.create_bucket_rest",
        lambda **kwargs: captured.update(kwargs) or {},
    )
    with _mock_token(None):
        result = create_bucket(
            CTX,
            bucket_name="b1",
            ram_quota_mb=256,
            bucket_type="membase",
            storage_backend="magma",
            replica_number=1,
            flush_enabled=True,
            durability_min_level="majority",
        )
    form = captured["form"]
    assert form["name"] == "b1"
    assert form["ramQuota"] == 256
    assert form["bucketType"] == "membase"
    assert form["storageBackend"] == "magma"
    assert form["replicaNumber"] == 1
    assert form["flushEnabled"] is True
    assert form["durabilityMinLevel"] == "majority"
    assert result["body"]["name"] == "b1"


def test_create_bucket_extra_forwarded_after_validation(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.bucket_admin.create_bucket_rest",
        lambda **kwargs: captured.update(kwargs) or {},
    )
    with _mock_token(None):
        create_bucket(
            CTX,
            bucket_name="b1",
            ram_quota_mb=256,
            extra={"maxTTL": 3600},
        )
    assert captured["form"]["maxTTL"] == 3600


# --------------------------------------------------------------------------
# update_bucket
# --------------------------------------------------------------------------


def test_update_bucket_requires_at_least_one_change():
    with _mock_token(None), pytest.raises(ValueError, match="at least one field"):
        update_bucket(CTX, bucket_name="b1")


def test_update_bucket_rejects_invalid_bucket_name():
    with _mock_token(None), pytest.raises(ValueError, match="Invalid bucket name"):
        update_bucket(CTX, bucket_name="b/../etc", ram_quota_mb=256)


def test_update_bucket_maps_snake_to_camel(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.bucket_admin.update_bucket_rest",
        lambda **kwargs: captured.update(kwargs) or {},
    )
    with _mock_token(None):
        update_bucket(
            CTX,
            bucket_name="b1",
            ram_quota_mb=512,
            replica_number=2,
        )
    assert captured["form"] == {"ramQuota": 512, "replicaNumber": 2}
    assert captured["bucket_name"] == "b1"


# --------------------------------------------------------------------------
# delete_bucket
# --------------------------------------------------------------------------


def test_delete_bucket_requires_confirm_true():
    with _mock_token(None), pytest.raises(ValueError, match="confirm=True"):
        delete_bucket(CTX, bucket_name="b1")


def test_delete_bucket_requires_matching_confirm_name():
    with _mock_token(None), pytest.raises(ValueError, match="confirm_name"):
        delete_bucket(CTX, bucket_name="b1", confirm=True, confirm_name="b2")


def test_delete_bucket_requires_confirm_name_explicitly():
    """confirm_name=None is not the same as matching the bucket name."""
    with _mock_token(None), pytest.raises(ValueError, match="confirm_name"):
        delete_bucket(CTX, bucket_name="b1", confirm=True)


def test_delete_bucket_succeeds_with_matching_confirm_name(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.bucket_admin.delete_bucket_rest",
        lambda **_: {"ok": True},
    )
    with _mock_token(None):
        result = delete_bucket(CTX, bucket_name="b1", confirm=True, confirm_name="b1")
    assert result == {"bucket": "b1", "deleted": True, "result": {"ok": True}}


# --------------------------------------------------------------------------
# flush_bucket
# --------------------------------------------------------------------------


def test_flush_bucket_requires_confirm_true():
    with _mock_token(None), pytest.raises(ValueError, match="confirm=True"):
        flush_bucket(CTX, bucket_name="b1")


def test_flush_bucket_succeeds_with_confirm(monkeypatch):
    monkeypatch.setattr("cb_mcp.tools.bucket_admin.flush_bucket_rest", lambda **_: {})
    with _mock_token(None):
        result = flush_bucket(CTX, bucket_name="b1", confirm=True)
    assert result["flushed"] is True


# --------------------------------------------------------------------------
# compact_bucket
# --------------------------------------------------------------------------


def test_compact_bucket_default_action_is_start(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.bucket_admin.compact_bucket_rest",
        lambda **kwargs: captured.update(kwargs) or {},
    )
    with _mock_token(None):
        compact_bucket(CTX, bucket_name="b1")
    assert captured["action"] == "start"


def test_compact_bucket_cancel_allowed(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "cb_mcp.tools.bucket_admin.compact_bucket_rest",
        lambda **kwargs: captured.update(kwargs) or {},
    )
    with _mock_token(None):
        compact_bucket(CTX, bucket_name="b1", action="cancel")
    assert captured["action"] == "cancel"


def test_compact_bucket_rejects_unknown_action():
    with _mock_token(None), pytest.raises(ValueError, match="action must be one of"):
        compact_bucket(CTX, bucket_name="b1", action="pause")


def test_compact_actions_allow_list_matches():
    """The tool's action allow-list must match the REST layer's."""
    assert frozenset({"start", "cancel"}) == ALLOWED_COMPACT_ACTIONS


# --------------------------------------------------------------------------
# load_sample_bucket
# --------------------------------------------------------------------------


def test_load_sample_bucket_rejects_unknown_sample():
    with (
        _mock_token(None),
        pytest.raises(ValueError, match="sample_name must be one of"),
    ):
        load_sample_bucket(CTX, sample_name="my-own-sample")


def test_load_sample_bucket_accepts_travel_sample(monkeypatch):
    monkeypatch.setattr(
        "cb_mcp.tools.bucket_admin.load_sample_bucket_rest",
        lambda **_: {},
    )
    with _mock_token(None):
        result = load_sample_bucket(CTX, sample_name="travel-sample")
    assert result["sample"] == "travel-sample"


def test_sample_bucket_allow_list_documented_set():
    """Documented Couchbase sample buckets 7.x/8.x. Guarding against
    accidental additions/removals."""
    assert (
        frozenset({"travel-sample", "beer-sample", "gamesim-sample"})
        == ALLOWED_SAMPLE_BUCKETS
    )


# --------------------------------------------------------------------------
# REST layer URL construction (httpx stubbed)
# --------------------------------------------------------------------------


def test_delete_bucket_rest_url_and_method(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(bucket_admin_rest.httpx, "Client", lambda **_: ctx_mgr)

    bucket_admin_rest.delete_bucket_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
        bucket_name="b1",
    )
    url = client.delete.call_args.args[0]
    assert url.endswith("/pools/default/buckets/b1")
    assert url.startswith("http://")


def test_tls_connection_string_uses_https_and_18091(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(bucket_admin_rest.httpx, "Client", lambda **_: ctx_mgr)

    bucket_admin_rest.delete_bucket_rest(
        connection_string="couchbases://mycluster",
        username="u",
        password="p",
        bucket_name="b1",
    )
    url = client.delete.call_args.args[0]
    assert url.startswith("https://")
    assert ":18091/" in url


def test_flush_rest_hits_correct_controller_path(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(bucket_admin_rest.httpx, "Client", lambda **_: ctx_mgr)

    bucket_admin_rest.flush_bucket_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
        bucket_name="b1",
    )
    url = client.post.call_args.args[0]
    assert url.endswith("/pools/default/buckets/b1/controller/doFlush")


@pytest.mark.parametrize(
    "action,controller",
    [("start", "compactBucket"), ("cancel", "cancelBucketCompaction")],
)
def test_compact_start_vs_cancel_paths(monkeypatch, action, controller):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(bucket_admin_rest.httpx, "Client", lambda **_: ctx_mgr)

    bucket_admin_rest.compact_bucket_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
        bucket_name="b1",
        action=action,
    )
    url = client.post.call_args.args[0]
    assert url.endswith(f"/pools/default/buckets/b1/controller/{controller}")


def test_load_sample_rest_posts_json_array(monkeypatch):
    ctx_mgr, client = _stub_httpx_client()
    monkeypatch.setattr(bucket_admin_rest.httpx, "Client", lambda **_: ctx_mgr)

    bucket_admin_rest.load_sample_bucket_rest(
        connection_string="couchbase://localhost",
        username="u",
        password="p",
        sample_name="travel-sample",
    )
    call_kwargs = client.post.call_args.kwargs
    assert json.loads(call_kwargs["content"]) == ["travel-sample"]
    assert call_kwargs["headers"]["content-type"] == "application/json"
