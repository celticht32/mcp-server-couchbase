"""
Unit tests for the scope/collection management tool bodies (parameter
validation, argument construction, confirmation gates, and write-scope
gating), exercised against stubbed CollectionManager / cluster objects
so no live Couchbase cluster is required.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.tools.collections_admin import (
    _require_write_scope,
    _timedelta_or_none,
    create_collection,
    create_scope,
    drop_collection,
    drop_scope,
    get_collection_settings,
    update_collection,
)
from cb_mcp.utils.constants import SCOPE_WRITE

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@contextmanager
def _mock_token(scopes):
    """Patch get_access_token at the collections_admin call site."""

    class _Tok:
        def __init__(self, scopes_):
            self.scopes = scopes_

    token = _Tok(scopes) if scopes is not None else None
    with patch("cb_mcp.tools.collections_admin.get_access_token", return_value=token):
        yield


def _ctx():
    """Minimal Context stub — the tools pass it to helper functions we stub."""
    return SimpleNamespace()


def _stub_cluster_bucket(monkeypatch, coll_mgr=None):
    """Stub get_cluster_connection and connect_to_bucket so we get a
    controllable CollectionManager mock back."""
    coll_mgr = coll_mgr or MagicMock()
    bucket = MagicMock()
    bucket.collections.return_value = coll_mgr
    cluster = MagicMock()
    monkeypatch.setattr(
        "cb_mcp.tools.collections_admin.get_cluster_connection",
        lambda ctx: cluster,
    )
    monkeypatch.setattr(
        "cb_mcp.tools.collections_admin.connect_to_bucket",
        lambda cluster, bucket_name: bucket,
    )
    return coll_mgr


CTX = _ctx()


# --------------------------------------------------------------------------
# _timedelta_or_none
# --------------------------------------------------------------------------


def test_timedelta_or_none_none():
    assert _timedelta_or_none(None) is None


def test_timedelta_or_none_zero():
    """Zero is 'no TTL', explicit — must return timedelta(0), not None."""
    assert _timedelta_or_none(0) == timedelta(seconds=0)


def test_timedelta_or_none_positive():
    assert _timedelta_or_none(3600) == timedelta(seconds=3600)


def test_timedelta_or_none_negative_rejected():
    with pytest.raises(ValueError, match="cannot be negative"):
        _timedelta_or_none(-1)


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
# create_scope
# --------------------------------------------------------------------------


def test_create_scope_calls_sdk_create_scope(monkeypatch):
    coll_mgr = _stub_cluster_bucket(monkeypatch)
    with _mock_token(None):
        result = create_scope(CTX, bucket_name="b1", scope_name="s1")
    coll_mgr.create_scope.assert_called_once_with("s1")
    assert result == {"bucket": "b1", "scope": "s1", "created": True}


def test_create_scope_propagates_sdk_error(monkeypatch):
    coll_mgr = _stub_cluster_bucket(monkeypatch)
    coll_mgr.create_scope.side_effect = RuntimeError("boom")
    with _mock_token(None), pytest.raises(RuntimeError, match="boom"):
        create_scope(CTX, bucket_name="b1", scope_name="s1")


# --------------------------------------------------------------------------
# drop_scope
# --------------------------------------------------------------------------


def test_drop_scope_requires_confirm_true():
    with _mock_token(None), pytest.raises(ValueError, match="confirm=True"):
        drop_scope(CTX, bucket_name="b1", scope_name="s1")


def test_drop_scope_requires_matching_confirm_name():
    with _mock_token(None), pytest.raises(ValueError, match="confirm_name"):
        drop_scope(
            CTX,
            bucket_name="b1",
            scope_name="s1",
            confirm=True,
            confirm_name="other",
        )


def test_drop_scope_requires_confirm_name_explicitly():
    with _mock_token(None), pytest.raises(ValueError, match="confirm_name"):
        drop_scope(CTX, bucket_name="b1", scope_name="s1", confirm=True)


def test_drop_scope_rejects_default_scope():
    with _mock_token(None), pytest.raises(ValueError, match="_default scope"):
        drop_scope(
            CTX,
            bucket_name="b1",
            scope_name="_default",
            confirm=True,
            confirm_name="_default",
        )


def test_drop_scope_succeeds_with_matching_name(monkeypatch):
    coll_mgr = _stub_cluster_bucket(monkeypatch)
    with _mock_token(None):
        result = drop_scope(
            CTX,
            bucket_name="b1",
            scope_name="s1",
            confirm=True,
            confirm_name="s1",
        )
    coll_mgr.drop_scope.assert_called_once_with("s1")
    assert result == {"bucket": "b1", "scope": "s1", "dropped": True}


# --------------------------------------------------------------------------
# create_collection
# --------------------------------------------------------------------------


def test_create_collection_minimal(monkeypatch):
    coll_mgr = _stub_cluster_bucket(monkeypatch)
    with _mock_token(None):
        result = create_collection(
            CTX,
            bucket_name="b1",
            scope_name="s1",
            collection_name="c1",
        )
    call = coll_mgr.create_collection.call_args
    spec = call.args[0]
    assert spec.name == "c1"
    assert spec.scope_name == "s1"
    assert result["created"] is True


def test_create_collection_with_ttl_and_history(monkeypatch):
    coll_mgr = _stub_cluster_bucket(monkeypatch)
    with _mock_token(None):
        result = create_collection(
            CTX,
            bucket_name="b1",
            scope_name="s1",
            collection_name="c1",
            max_expiry_seconds=3600,
            history=True,
        )
    spec = coll_mgr.create_collection.call_args.args[0]
    assert spec.max_expiry == timedelta(seconds=3600)
    assert spec.history is True
    assert result["max_expiry_seconds"] == 3600
    assert result["history"] is True


def test_create_collection_zero_ttl_means_no_expiry(monkeypatch):
    """max_expiry_seconds=0 means 'no TTL', distinct from None ('inherit')."""
    coll_mgr = _stub_cluster_bucket(monkeypatch)
    with _mock_token(None):
        create_collection(
            CTX,
            bucket_name="b1",
            scope_name="s1",
            collection_name="c1",
            max_expiry_seconds=0,
        )
    spec = coll_mgr.create_collection.call_args.args[0]
    assert spec.max_expiry == timedelta(seconds=0)


def test_create_collection_negative_ttl_rejected():
    with _mock_token(None), pytest.raises(ValueError, match="cannot be negative"):
        create_collection(
            CTX,
            bucket_name="b1",
            scope_name="s1",
            collection_name="c1",
            max_expiry_seconds=-5,
        )


# --------------------------------------------------------------------------
# drop_collection
# --------------------------------------------------------------------------


def test_drop_collection_requires_confirm_true():
    with _mock_token(None), pytest.raises(ValueError, match="confirm=True"):
        drop_collection(CTX, bucket_name="b1", scope_name="s1", collection_name="c1")


def test_drop_collection_requires_matching_confirm_name():
    with _mock_token(None), pytest.raises(ValueError, match="confirm_name"):
        drop_collection(
            CTX,
            bucket_name="b1",
            scope_name="s1",
            collection_name="c1",
            confirm=True,
            confirm_name="c2",
        )


def test_drop_collection_rejects_default_default():
    with _mock_token(None), pytest.raises(ValueError, match="_default"):
        drop_collection(
            CTX,
            bucket_name="b1",
            scope_name="_default",
            collection_name="_default",
            confirm=True,
            confirm_name="_default",
        )


def test_drop_collection_allows_default_scope_non_default_collection(monkeypatch):
    """A user-created collection in the _default scope IS droppable."""
    coll_mgr = _stub_cluster_bucket(monkeypatch)
    with _mock_token(None):
        result = drop_collection(
            CTX,
            bucket_name="b1",
            scope_name="_default",
            collection_name="my_coll",
            confirm=True,
            confirm_name="my_coll",
        )
    coll_mgr.drop_collection.assert_called_once()
    assert result["dropped"] is True


def test_drop_collection_succeeds(monkeypatch):
    coll_mgr = _stub_cluster_bucket(monkeypatch)
    with _mock_token(None):
        result = drop_collection(
            CTX,
            bucket_name="b1",
            scope_name="s1",
            collection_name="c1",
            confirm=True,
            confirm_name="c1",
        )
    spec = coll_mgr.drop_collection.call_args.args[0]
    assert spec.name == "c1"
    assert spec.scope_name == "s1"
    assert result["dropped"] is True


# --------------------------------------------------------------------------
# update_collection
# --------------------------------------------------------------------------


def test_update_collection_requires_at_least_one_change():
    with _mock_token(None), pytest.raises(ValueError, match="at least one"):
        update_collection(CTX, bucket_name="b1", scope_name="s1", collection_name="c1")


def test_update_collection_ttl_only(monkeypatch):
    coll_mgr = _stub_cluster_bucket(monkeypatch)
    with _mock_token(None):
        result = update_collection(
            CTX,
            bucket_name="b1",
            scope_name="s1",
            collection_name="c1",
            max_expiry_seconds=7200,
        )
    spec = coll_mgr.update_collection.call_args.args[0]
    assert spec.max_expiry == timedelta(seconds=7200)
    assert result["updated"] is True


def test_update_collection_history_only(monkeypatch):
    coll_mgr = _stub_cluster_bucket(monkeypatch)
    with _mock_token(None):
        update_collection(
            CTX,
            bucket_name="b1",
            scope_name="s1",
            collection_name="c1",
            history=False,
        )
    spec = coll_mgr.update_collection.call_args.args[0]
    assert spec.history is False


# --------------------------------------------------------------------------
# get_collection_settings
# --------------------------------------------------------------------------


def _fake_scope_spec(name, collections):
    """Build a minimal object shaped like the SDK's ScopeSpec."""
    scope = SimpleNamespace(name=name, collections=collections)
    return scope


def _fake_collection_spec(name, max_expiry=None, history=None):
    c = SimpleNamespace(name=name)
    if max_expiry is not None:
        c.max_expiry = max_expiry
    if history is not None:
        c.history = history
    return c


def test_get_collection_settings_returns_ttl_and_history(monkeypatch):
    coll = _fake_collection_spec("c1", max_expiry=timedelta(seconds=1800), history=True)
    scope = _fake_scope_spec("s1", [coll])
    coll_mgr = MagicMock()
    coll_mgr.get_all_scopes.return_value = [scope]
    _stub_cluster_bucket(monkeypatch, coll_mgr=coll_mgr)

    result = get_collection_settings(
        CTX, bucket_name="b1", scope_name="s1", collection_name="c1"
    )
    assert result == {
        "bucket": "b1",
        "scope": "s1",
        "collection": "c1",
        "max_expiry_seconds": 1800,
        "history": True,
    }


def test_get_collection_settings_none_max_expiry(monkeypatch):
    """When the SDK returns no max_expiry, we return None (not 0)."""
    coll = _fake_collection_spec("c1", history=False)
    scope = _fake_scope_spec("s1", [coll])
    coll_mgr = MagicMock()
    coll_mgr.get_all_scopes.return_value = [scope]
    _stub_cluster_bucket(monkeypatch, coll_mgr=coll_mgr)

    result = get_collection_settings(
        CTX, bucket_name="b1", scope_name="s1", collection_name="c1"
    )
    assert result["max_expiry_seconds"] is None
    assert result["history"] is False


def test_get_collection_settings_scope_not_found(monkeypatch):
    coll_mgr = MagicMock()
    coll_mgr.get_all_scopes.return_value = [_fake_scope_spec("other", [])]
    _stub_cluster_bucket(monkeypatch, coll_mgr=coll_mgr)

    with pytest.raises(ValueError, match="Scope 's1' not found"):
        get_collection_settings(
            CTX, bucket_name="b1", scope_name="s1", collection_name="c1"
        )


def test_get_collection_settings_collection_not_found(monkeypatch):
    scope = _fake_scope_spec("s1", [_fake_collection_spec("other")])
    coll_mgr = MagicMock()
    coll_mgr.get_all_scopes.return_value = [scope]
    _stub_cluster_bucket(monkeypatch, coll_mgr=coll_mgr)

    with pytest.raises(ValueError, match="Collection 'c1' not found"):
        get_collection_settings(
            CTX, bucket_name="b1", scope_name="s1", collection_name="c1"
        )
