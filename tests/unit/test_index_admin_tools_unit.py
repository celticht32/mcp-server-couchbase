"""
Unit tests for the index-management tool bodies (statement construction and
write-scope gating), exercised against stubbed cluster and token modules so
no live Couchbase cluster is required.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from cb_mcp.tools.index_admin import (
    build_deferred_indexes,
    create_index,
    drop_index,
)


@contextmanager
def _mock_token(scopes):
    """Patch get_access_token at the index_admin call site.

    Pass a list of scopes for an authenticated token, or None for no token
    (stdio / OAuth disabled).
    """

    class _Tok:
        def __init__(self, scopes_):
            self.scopes = scopes_

    token = _Tok(scopes) if scopes is not None else None
    with patch("cb_mcp.tools.index_admin.get_access_token", return_value=token):
        yield


@pytest.fixture(autouse=True)
def _stub_cluster_query(monkeypatch):
    """Patch run_cluster_query at the index_admin call site so statement
    construction can be tested without a live cluster. Records nothing;
    tests assert on the returned ``statement`` field the tool echoes back."""
    monkeypatch.setattr(
        "cb_mcp.tools.index_admin.run_cluster_query",
        lambda ctx, statement, **kw: [{"ok": True}],
    )
    yield


CTX = object()  # tools never introspect ctx directly; they pass it through


# --------------------------------------------------------------------------
# create_index - structured path
# --------------------------------------------------------------------------


def test_create_secondary_index_builds_expected_statement():
    out = create_index(
        CTX,
        index_name="idx_name",
        bucket_name="travel-sample",
        scope_name="inventory",
        collection_name="airline",
        fields=["name", "country"],
    )
    assert out["statement"] == (
        "CREATE INDEX `idx_name` ON "
        "`travel-sample`.`inventory`.`airline` (`name`, `country`)"
    )


def test_create_primary_index_with_name():
    out = create_index(CTX, index_name="pk", bucket_name="b", is_primary=True)
    assert out["statement"] == "CREATE PRIMARY INDEX `pk` ON `b`.`_default`.`_default`"


def test_create_primary_index_without_name():
    out = create_index(CTX, bucket_name="b", is_primary=True)
    assert out["statement"] == "CREATE PRIMARY INDEX ON `b`.`_default`.`_default`"


def test_create_with_num_replica_and_defer():
    out = create_index(
        CTX,
        index_name="i",
        bucket_name="b",
        fields=["x"],
        num_replica=2,
        defer_build=True,
    )
    assert out["statement"].endswith('WITH {"num_replica": 2, "defer_build": true}')


def test_create_missing_bucket_raises():
    with pytest.raises(ValueError, match="bucket_name is required"):
        create_index(CTX, index_name="i", fields=["x"])


def test_create_secondary_missing_fields_raises():
    with pytest.raises(ValueError, match="fields are required"):
        create_index(CTX, index_name="i", bucket_name="b")


def test_create_secondary_missing_name_raises():
    with pytest.raises(ValueError, match="index_name is required"):
        create_index(CTX, bucket_name="b", fields=["x"])


# --------------------------------------------------------------------------
# create_index - raw statement path
# --------------------------------------------------------------------------


def test_create_raw_statement_allowed():
    out = create_index(CTX, statement="CREATE INDEX i ON b.s.c (x)")
    assert out["statement"] == "CREATE INDEX i ON b.s.c (x)"


def test_create_raw_statement_rejected():
    with pytest.raises(ValueError, match="index-create DDL"):
        create_index(CTX, statement="DELETE FROM b.s.c")


# --------------------------------------------------------------------------
# drop_index
# --------------------------------------------------------------------------


def test_drop_secondary_index_statement():
    out = drop_index(
        CTX, index_name="i", bucket_name="b", scope_name="s", collection_name="c"
    )
    assert out["statement"] == "DROP INDEX `i` ON `b`.`s`.`c`"


def test_drop_primary_index_statement():
    out = drop_index(CTX, bucket_name="b", is_primary=True)
    assert out["statement"] == "DROP PRIMARY INDEX ON `b`.`_default`.`_default`"


def test_drop_raw_statement_rejected():
    with pytest.raises(ValueError, match="DROP INDEX"):
        drop_index(CTX, statement="DROP SCOPE b.s")


def test_drop_missing_name_raises():
    with pytest.raises(ValueError, match="index_name is required"):
        drop_index(CTX, bucket_name="b")


# --------------------------------------------------------------------------
# build_deferred_indexes
# --------------------------------------------------------------------------


def test_build_bucket_level():
    out = build_deferred_indexes(CTX, bucket_name="b", index_names=["i1", "i2"])
    assert out["statement"] == "BUILD INDEX ON `b` (`i1`, `i2`)"


def test_build_collection_level():
    out = build_deferred_indexes(
        CTX,
        bucket_name="b",
        index_names=["i1"],
        scope_name="s",
        collection_name="c",
    )
    assert out["statement"] == "BUILD INDEX ON `b`.`s`.`c` (`i1`)"


def test_build_partial_keyspace_raises():
    with pytest.raises(ValueError, match="must be provided together"):
        build_deferred_indexes(CTX, bucket_name="b", index_names=["i"], scope_name="s")


def test_build_empty_names_raises():
    with pytest.raises(ValueError, match="at least one"):
        build_deferred_indexes(CTX, bucket_name="b", index_names=[])


# --------------------------------------------------------------------------
# write-scope gating (mirrors run_sql_plus_plus_query semantics)
# --------------------------------------------------------------------------


def test_no_token_allows_write():
    # stdio / OAuth disabled: no token in context -> no scope enforcement
    with _mock_token(None):
        out = create_index(CTX, bucket_name="b", is_primary=True)
    assert out["statement"].startswith("CREATE PRIMARY INDEX")


def test_token_with_write_scope_allows():
    with _mock_token(["couchbase-mcp:write"]):
        out = drop_index(CTX, bucket_name="b", is_primary=True)
    assert out["statement"].startswith("DROP PRIMARY INDEX")


def test_token_without_write_scope_denied():
    with (
        _mock_token(["couchbase-mcp:read"]),
        pytest.raises(PermissionError, match="requires the 'couchbase-mcp:write'"),
    ):
        create_index(CTX, bucket_name="b", is_primary=True)


def test_build_denied_without_write_scope():
    with _mock_token([]), pytest.raises(PermissionError):
        build_deferred_indexes(CTX, bucket_name="b", index_names=["i"])
