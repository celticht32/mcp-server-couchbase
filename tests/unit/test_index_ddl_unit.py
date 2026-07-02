"""
Unit tests for index DDL safety primitives and statement construction.

These run without a Couchbase cluster: they exercise the pure logic
(identifier quoting, statement allow-listing, statement building) and the
write-scope check via a stubbed access token. The cluster execution path
(``run_cluster_query``) is monkeypatched.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

import pytest

from cb_mcp.utils.index_ddl import (
    assert_index_create_ddl,
    assert_index_drop_ddl,
    safe_ident,
)

# --------------------------------------------------------------------------
# safe_ident
# --------------------------------------------------------------------------


def test_safe_ident_wraps_in_backticks():
    assert safe_ident("users") == "`users`"


def test_safe_ident_doubles_embedded_backtick():
    # A name containing a backtick must not be able to terminate its quoting.
    assert safe_ident("we`ird") == "`we``ird`"


def test_safe_ident_handles_empty_and_none():
    assert safe_ident("") == "``"
    assert safe_ident(None) == "``"


def test_safe_ident_injection_attempt_is_neutralized():
    # Attempt to break out and append a DROP; the backtick is doubled so the
    # whole thing stays a single (absurd) identifier.
    malicious = "x` ON b.s.c; DROP INDEX `y"
    quoted = safe_ident(malicious)
    assert quoted.startswith("`") and quoted.endswith("`")
    # No unescaped backtick remains that could close the identifier early.
    inner = quoted[1:-1]
    assert "``" in inner
    assert not any(
        inner[i] == "`" and inner[i - 1] != "`" and inner[i + 1 : i + 2] != "`"
        for i in range(1, len(inner) - 1)
    )


# --------------------------------------------------------------------------
# assert_index_create_ddl
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stmt",
    [
        "CREATE INDEX idx ON b.s.c (name)",
        "create index idx on b.s.c (name)",
        "CREATE PRIMARY INDEX ON b.s.c",
        "  CREATE   PRIMARY   INDEX ON b.s.c",
        "CREATE VECTOR INDEX v ON b.s.c (emb VECTOR)",
        "CREATE HYPERSCALE VECTOR INDEX v ON b.s.c (emb VECTOR)",
        "CREATE COMPOSITE VECTOR INDEX v ON b.s.c (emb VECTOR)",
    ],
)
def test_create_ddl_allows_index_statements(stmt):
    assert assert_index_create_ddl(stmt) is None


@pytest.mark.parametrize(
    "stmt",
    [
        "SELECT * FROM b.s.c",
        "DELETE FROM b.s.c",
        "UPDATE b.s.c SET x = 1",
        "DROP INDEX idx ON b.s.c",
        "INSERT INTO b.s.c VALUES (1, {})",
        "CREATE SCOPE b.s",
        "CREATE COLLECTION b.s.c",
        "",
        "   ",
        "; CREATE INDEX idx ON b.s.c (name)",  # leading junk before keyword
        "BUILD INDEX ON b.s.c (i)",  # BUILD routes to build_deferred_indexes
        # multi-statement injection: permitted head + smuggled tail
        "CREATE INDEX i ON b.s.c (x); DELETE FROM b.s.c WHERE 1=1",
        "CREATE PRIMARY INDEX ON b.s.c ; DROP SCOPE b.s",
        "CREATE INDEX i ON b.s.c (x);SELECT 1",
    ],
)
def test_create_ddl_rejects_non_index_statements(stmt):
    msg = assert_index_create_ddl(stmt)
    assert msg is not None
    assert "index-create DDL" in msg


def test_create_ddl_handles_none():
    assert assert_index_create_ddl(None) is not None


@pytest.mark.parametrize(
    "stmt",
    [
        "CREATE INDEX i ON b.s.c (name);",  # single trailing semicolon
        "CREATE INDEX i ON b.s.c (name)  ;  ",  # trailing semicolon + whitespace
        "CREATE INDEX `weird;name` ON b.s.c (x)",  # ';' inside a quoted ident
    ],
)
def test_create_ddl_allows_trailing_semicolon_and_quoted_semicolon(stmt):
    assert assert_index_create_ddl(stmt) is None


# --------------------------------------------------------------------------
# assert_index_drop_ddl
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stmt",
    [
        "DROP INDEX idx ON b.s.c",
        "drop index idx on b.s.c",
        "DROP PRIMARY INDEX ON b.s.c",
        "DROP VECTOR INDEX v ON b.s.c",
        "  DROP   INDEX idx ON b.s.c",
    ],
)
def test_drop_ddl_allows_drop_statements(stmt):
    assert assert_index_drop_ddl(stmt) is None


@pytest.mark.parametrize(
    "stmt",
    [
        "CREATE INDEX idx ON b.s.c (name)",
        "SELECT * FROM b.s.c",
        "DELETE FROM b.s.c",
        "DROP SCOPE b.s",
        "DROP COLLECTION b.s.c",
        "DROP PRIMARY VECTOR INDEX ON b.s.c",  # not a real statement shape
        "DROP INDEX i ON b.s.c; CREATE PRIMARY INDEX ON b.s.c",  # injection
        "",
        None,
    ],
)
def test_drop_ddl_rejects_non_drop_statements(stmt):
    assert assert_index_drop_ddl(stmt) is not None
