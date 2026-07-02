"""
Unit tests for the GSI settings tools and REST helper pure logic.

The tools' network calls (get_gsi_settings / set_gsi_settings) are
monkeypatched so these tests exercise parameter mapping and gating without a
cluster. The REST helper's pure functions (_form_value, _settings_base) are
tested directly.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from cb_mcp.tools import index_admin
from cb_mcp.utils import index_settings


@contextmanager
def _mock_token(scopes):
    class _Tok:
        def __init__(self, scopes_):
            self.scopes = scopes_

    token = _Tok(scopes) if scopes is not None else None
    with patch("cb_mcp.tools.index_admin.get_access_token", return_value=token):
        yield


@pytest.fixture(autouse=True)
def _stub_get_settings(monkeypatch):
    """Patch get_settings so settings tools don't require a live AppContext."""
    monkeypatch.setattr(
        "cb_mcp.tools.index_admin.get_settings",
        lambda ctx: {
            "connection_string": "couchbase://localhost",
            "username": "u",
            "password": "p",
            "ca_cert_path": None,
        },
    )
    yield


CTX = object()


# --------------------------------------------------------------------------
# REST helper pure logic
# --------------------------------------------------------------------------


def test_form_value_bools_lowercased():
    assert index_settings._form_value(True) == "true"
    assert index_settings._form_value(False) == "false"


def test_form_value_ints_and_strings():
    assert index_settings._form_value(4) == "4"
    assert index_settings._form_value("plasma") == "plasma"


def test_settings_base_non_tls():
    assert index_settings._settings_base("couchbase://localhost") == ("http", 8091)


def test_settings_base_tls():
    assert index_settings._settings_base("couchbases://cb.example") == (
        "https",
        18091,
    )


def test_set_gsi_settings_rejects_empty():
    with pytest.raises(ValueError, match="at least one setting"):
        index_settings.set_gsi_settings("couchbase://h", "u", "p", params={})


def test_set_gsi_settings_rejects_all_none():
    with pytest.raises(ValueError, match="no non-null settings"):
        index_settings.set_gsi_settings(
            "couchbase://h", "u", "p", params={"numReplica": None}
        )


# --------------------------------------------------------------------------
# admin_index_settings_get
# --------------------------------------------------------------------------


def test_settings_get_returns_settings(monkeypatch):
    captured = {}

    def fake_get(**kwargs):
        captured.update(kwargs)
        return {"indexerThreads": 4, "logLevel": "info"}

    monkeypatch.setattr(index_admin, "get_gsi_settings", fake_get)
    out = index_admin.admin_index_settings_get(CTX)
    assert out == {"indexerThreads": 4, "logLevel": "info"}
    assert captured["connection_string"] == "couchbase://localhost"


# --------------------------------------------------------------------------
# admin_index_settings_set - param mapping
# --------------------------------------------------------------------------


def test_settings_set_maps_named_params(monkeypatch):
    captured = {}

    def fake_set(**kwargs):
        captured.update(kwargs)
        return {"applied": True}

    monkeypatch.setattr(index_admin, "set_gsi_settings", fake_set)
    index_admin.admin_index_settings_set(
        CTX,
        indexer_threads=8,
        log_level="verbose",
        redistribute_indexes=False,
    )
    assert captured["params"] == {
        "indexerThreads": 8,
        "logLevel": "verbose",
        "redistributeIndexes": False,
    }


def test_settings_set_merges_extra(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        index_admin,
        "set_gsi_settings",
        lambda **kw: captured.update(kw) or {},
    )
    # extra keys must be documented camelCase GSI keys; enableShardAffinity is
    # valid but has no named parameter in an older tool version, so it is a
    # representative forward-compat use of the escape hatch.
    index_admin.admin_index_settings_set(
        CTX, num_replica=2, extra={"enableShardAffinity": True}
    )
    assert captured["params"] == {"numReplica": 2, "enableShardAffinity": True}


def test_settings_set_rejects_unknown_extra_key(monkeypatch):
    monkeypatch.setattr(index_admin, "set_gsi_settings", lambda **kw: {})
    with pytest.raises(ValueError, match="Unknown GSI setting key"):
        index_admin.admin_index_settings_set(CTX, extra={"arbitraryKey": "x"})


def test_settings_set_requires_at_least_one(monkeypatch):
    monkeypatch.setattr(index_admin, "set_gsi_settings", lambda **kw: {})
    with pytest.raises(ValueError, match="at least one setting"):
        index_admin.admin_index_settings_set(CTX)


def test_settings_set_omits_none_values(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        index_admin,
        "set_gsi_settings",
        lambda **kw: captured.update(kw) or {},
    )
    index_admin.admin_index_settings_set(CTX, storage_mode="plasma")
    assert captured["params"] == {"storageMode": "plasma"}


# --------------------------------------------------------------------------
# write-scope gating on set (get is read-only, not gated)
# --------------------------------------------------------------------------


def test_settings_set_denied_without_write_scope(monkeypatch):
    monkeypatch.setattr(index_admin, "set_gsi_settings", lambda **kw: {})
    with _mock_token(["couchbase-mcp:read"]), pytest.raises(PermissionError):
        index_admin.admin_index_settings_set(CTX, indexer_threads=4)


def test_settings_set_allowed_with_write_scope(monkeypatch):
    monkeypatch.setattr(index_admin, "set_gsi_settings", lambda **kw: {"ok": True})
    with _mock_token(["couchbase-mcp:write"]):
        out = index_admin.admin_index_settings_set(CTX, indexer_threads=4)
    assert out == {"ok": True}
