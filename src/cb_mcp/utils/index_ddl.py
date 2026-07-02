"""
Index DDL safety primitives.

Identifier quoting and statement allow-listing for the index-management
tools. These guard the two paths by which a caller can reach index DDL:

1. Structured helper fields (bucket/scope/collection/index names, key
   fields). Every identifier is backtick-quoted via :func:`safe_ident`
   before interpolation, so a name containing a backtick cannot break out
   of its quoting and inject arbitrary SQL++.

2. A raw ``statement`` string. This is convenient but dangerous, so it is
   constrained by :func:`assert_index_create_ddl` /
   :func:`assert_index_drop_ddl`, which reject anything that is not the
   expected index-DDL shape. This prevents the raw path from becoming a
   general SQL++ execution channel that bypasses read-only and
   admin-write gating.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

import re

# Allow-list patterns for the raw ``statement`` path. Anchored at the start so
# a statement must *begin* with the permitted keyword. Note the anchor alone is
# not sufficient: a statement like "CREATE INDEX ... ; DELETE FROM ..." begins
# with a permitted keyword but carries a second statement. ``_is_single_statement``
# below rejects any embedded statement separator so the raw path cannot be used
# to smuggle a trailing DML/DDL statement past the allow-list.
_INDEX_CREATE_RE = re.compile(
    r"""^\s*
    (
        CREATE\s+(PRIMARY\s+)?INDEX
        | CREATE\s+(?:HYPERSCALE\s+|COMPOSITE\s+)?VECTOR\s+INDEX
    )
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)

_INDEX_BUILD_RE = re.compile(r"""^\s*BUILD\s+INDEX\b""", re.IGNORECASE | re.VERBOSE)

_INDEX_DROP_RE = re.compile(
    r"""^\s*DROP\s+((PRIMARY|VECTOR)\s+)?INDEX\b""",
    re.IGNORECASE | re.VERBOSE,
)


def _is_single_statement(statement: str) -> bool:
    """True if *statement* contains no embedded statement separator.

    SQL++ separates statements with ``;``. A trailing semicolon (optionally
    followed by whitespace) is allowed; a semicolon with anything non-blank
    after it means a second statement is present and the input is rejected.
    Backtick-quoted identifiers may legitimately contain a ``;`` inside the
    quotes, so those spans are removed before the check to avoid false
    positives on names like ``` `weird;name` ```.
    """
    # Strip backtick-quoted spans (identifiers) so a ';' inside a quoted name
    # is not mistaken for a statement separator. Doubled backticks are escapes.
    without_idents = re.sub(r"`(?:[^`]|``)*`", "", statement)
    stripped = without_idents.rstrip()
    if stripped.endswith(";"):
        stripped = stripped[:-1]
    return ";" not in stripped


def safe_ident(segment: str) -> str:
    """Backtick-quote and escape an identifier for SQL++.

    Couchbase SQL++ escapes an embedded backtick by doubling it, so a
    caller-supplied name can never terminate its own quoting. Applied to
    every bucket / scope / collection / index / field name before it is
    interpolated into a statement.
    """
    return "`" + (segment or "").replace("`", "``") + "`"


def assert_index_create_ddl(statement: str) -> str | None:
    """Return an error message if *statement* is not permitted index-create DDL.

    Returns ``None`` when the statement is a single CREATE INDEX / CREATE
    PRIMARY INDEX / CREATE [HYPERSCALE|COMPOSITE] VECTOR INDEX statement. Any
    other SQL++ - including a permitted statement followed by a second,
    smuggled statement - is rejected so the raw ``statement`` path cannot be
    used to run arbitrary queries or DML. (BUILD INDEX is handled by
    ``build_deferred_indexes``, not this create path.)
    """
    stmt = statement or ""
    if not _INDEX_CREATE_RE.match(stmt) or not _is_single_statement(stmt):
        return (
            "The `statement` parameter only accepts a single index-create DDL "
            "statement (CREATE INDEX, CREATE PRIMARY INDEX, or CREATE "
            "[HYPERSCALE|COMPOSITE] VECTOR INDEX), with no trailing statement. "
            "Use the helper fields (index_name, bucket_name, fields, etc.) for "
            "structured creation, or run other SQL++ via run_sql_plus_plus_query."
        )
    return None


def assert_index_drop_ddl(statement: str) -> str | None:
    """Return an error message if *statement* is not permitted index-drop DDL.

    Returns ``None`` when the statement is a single DROP INDEX, DROP PRIMARY
    INDEX, or DROP VECTOR INDEX statement. Any other SQL++ - including a
    trailing smuggled statement - is rejected.
    """
    stmt = statement or ""
    if not _INDEX_DROP_RE.match(stmt) or not _is_single_statement(stmt):
        return (
            "The `statement` parameter only accepts a single DROP INDEX, DROP "
            "PRIMARY INDEX, or DROP VECTOR INDEX statement, with no trailing "
            "statement. Use the helper fields for structured drops, or run "
            "other SQL++ via run_sql_plus_plus_query."
        )
    return None
