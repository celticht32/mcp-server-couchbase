"""
Tools for index management (DDL).

These tools create, drop, and build GSI indexes and read/update Index
Service settings. They complement the read-only ``index.py`` tools
(``list_indexes`` and ``get_index_advisor_recommendations``): the advisor
recommends indexes, ``list_indexes`` shows them, and these tools act on
them.

Write gating
------------
Index DDL mutates cluster structure. These tools are loaded only when the
server is *not* in read-only mode AND admin-write mode is enabled (see
``tools/__init__.py`` ``get_tools`` and ``ADMIN_WRITE_TOOLS``). When an
OAuth token is present, the caller must additionally hold the write scope;
this mirrors the scope enforcement in ``run_sql_plus_plus_query`` so a
read-scoped token cannot mutate indexes even when admin-write mode is on.

DDL is executed through ``run_cluster_query`` rather than
``run_sql_plus_plus_query``. The latter classifies ``CREATE``/``DROP``/
``BUILD INDEX`` as structure modifications and blocks them under read-only
mode; routing DDL through the cluster path keeps load-time gating
(``ADMIN_WRITE_TOOLS`` + admin-write mode + scope) as the single, explicit
enforcement point instead of double-gating.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

import logging
from typing import Any

from fastmcp import Context
from fastmcp.server.dependencies import get_access_token

from ..utils.config import get_settings
from ..utils.constants import MCP_SERVER_NAME, SCOPE_WRITE
from ..utils.index_ddl import (
    assert_index_create_ddl,
    assert_index_drop_ddl,
    safe_ident,
)
from ..utils.index_settings import get_gsi_settings, set_gsi_settings
from .query import run_cluster_query

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.index_admin")


def _require_write_scope() -> None:
    """Raise ``PermissionError`` if a token is present but lacks the write scope.

    No-op when no token is in context (stdio transport or OAuth disabled),
    matching the behavior of ``run_sql_plus_plus_query`` so the same tool
    body serves authenticated HTTP and unauthenticated stdio without
    branching at registration time.
    """
    token = get_access_token()
    if token is not None and SCOPE_WRITE not in (token.scopes or []):
        held = sorted(set(token.scopes or []))
        msg = f"Index DDL requires the '{SCOPE_WRITE}' scope; token scopes are {held}."
        logger.warning(msg)
        raise PermissionError(msg)


def create_index(
    ctx: Context,
    statement: str | None = None,
    index_name: str | None = None,
    bucket_name: str | None = None,
    scope_name: str | None = None,
    collection_name: str | None = None,
    fields: list[str] | None = None,
    is_primary: bool = False,
    num_replica: int | None = None,
    defer_build: bool = False,
) -> dict[str, Any]:
    """Create a GSI index.

    Two ways to call this:

    - Raw ``statement``: pass a full SQL++ index-create statement. Only
      CREATE INDEX / CREATE PRIMARY INDEX / BUILD INDEX / CREATE
      [HYPERSCALE|COMPOSITE] VECTOR INDEX are accepted; any other SQL++ is
      rejected.
    - Structured fields: provide ``bucket_name`` (and optionally
      ``scope_name`` / ``collection_name``, defaulting to ``_default``) with
      either ``is_primary=True`` or an ``index_name`` plus ``fields``.
      Optional ``num_replica`` and ``defer_build`` become a WITH clause.

    Returns a dict with the executed statement and result rows.
    """
    _require_write_scope()

    if statement:
        invalid = assert_index_create_ddl(statement)
        if invalid:
            raise ValueError(invalid)
        stmt = statement
    else:
        if not bucket_name:
            raise ValueError("bucket_name is required when statement is not provided")
        bucket = safe_ident(bucket_name)
        scope = safe_ident(scope_name or "_default")
        coll = safe_ident(collection_name or "_default")

        if is_primary:
            idx = safe_ident(index_name) if index_name else ""
            target = f"{idx} " if idx else ""
            stmt = f"CREATE PRIMARY INDEX {target}ON {bucket}.{scope}.{coll}"
        else:
            if not index_name:
                raise ValueError("index_name is required for non-primary indexes")
            if not fields:
                raise ValueError("fields are required for non-primary indexes")
            idx = safe_ident(index_name)
            field_list = ", ".join(safe_ident(f) for f in fields)
            stmt = f"CREATE INDEX {idx} ON {bucket}.{scope}.{coll} ({field_list})"

        withs = []
        if num_replica is not None:
            withs.append(f'"num_replica": {int(num_replica)}')
        if defer_build:
            withs.append('"defer_build": true')
        if withs:
            stmt += " WITH {" + ", ".join(withs) + "}"

    logger.info("Executing index create")
    rows = run_cluster_query(ctx, stmt)
    return {"statement": stmt, "results": rows}


def drop_index(
    ctx: Context,
    statement: str | None = None,
    index_name: str | None = None,
    bucket_name: str | None = None,
    scope_name: str | None = None,
    collection_name: str | None = None,
    is_primary: bool = False,
) -> dict[str, Any]:
    """Drop a GSI index.

    Two ways to call this:

    - Raw ``statement``: a full DROP INDEX / DROP PRIMARY INDEX / DROP VECTOR
      INDEX statement. Any other SQL++ is rejected.
    - Structured fields: ``bucket_name`` (and optional ``scope_name`` /
      ``collection_name``) with either ``is_primary=True`` or ``index_name``.

    Returns a dict with the executed statement and result rows.
    """
    _require_write_scope()

    if statement:
        invalid = assert_index_drop_ddl(statement)
        if invalid:
            raise ValueError(invalid)
        stmt = statement
    else:
        if not bucket_name:
            raise ValueError("bucket_name is required when statement is not provided")
        bucket = safe_ident(bucket_name)
        scope = safe_ident(scope_name or "_default")
        coll = safe_ident(collection_name or "_default")

        if is_primary:
            stmt = f"DROP PRIMARY INDEX ON {bucket}.{scope}.{coll}"
        else:
            if not index_name:
                raise ValueError("index_name is required for non-primary drop")
            idx = safe_ident(index_name)
            stmt = f"DROP INDEX {idx} ON {bucket}.{scope}.{coll}"

    logger.info("Executing index drop")
    rows = run_cluster_query(ctx, stmt)
    return {"statement": stmt, "results": rows}


def build_deferred_indexes(
    ctx: Context,
    bucket_name: str,
    index_names: list[str],
    scope_name: str | None = None,
    collection_name: str | None = None,
) -> dict[str, Any]:
    """Build one or more deferred GSI indexes.

    ``bucket_name`` and ``index_names`` are required. Provide both
    ``scope_name`` and ``collection_name`` to build within a specific
    collection (Couchbase 7+); omit both for a bucket-level build.

    Returns a dict with the executed statement and result rows.
    """
    _require_write_scope()

    if not index_names:
        raise ValueError("index_names must contain at least one index name")

    bucket = safe_ident(bucket_name)
    if scope_name and collection_name:
        keyspace = f"{bucket}.{safe_ident(scope_name)}.{safe_ident(collection_name)}"
    elif scope_name or collection_name:
        raise ValueError("scope_name and collection_name must be provided together")
    else:
        keyspace = bucket

    names = ", ".join(safe_ident(n) for n in index_names)
    stmt = f"BUILD INDEX ON {keyspace} ({names})"

    logger.info("Executing deferred index build")
    rows = run_cluster_query(ctx, stmt)
    return {"statement": stmt, "results": rows}


# --------------------------------------------------------------------------
# GSI settings (cluster-manager /settings/indexes, port 8091/18091)
# --------------------------------------------------------------------------

# Named GSI settings exposed as explicit tool parameters. Maps the tool's
# snake_case argument to the endpoint's camelCase form key. These are the
# complete set of keys documented for POST /settings/indexes (Couchbase Server
# 8.0). Verified against docs 2026-07-02.
_GSI_SETTING_KEYS: dict[str, str] = {
    "indexer_threads": "indexerThreads",
    "log_level": "logLevel",
    "max_rollback_points": "maxRollbackPoints",
    "storage_mode": "storageMode",
    "num_replica": "numReplica",
    "redistribute_indexes": "redistributeIndexes",
    "enable_page_bloom_filter": "enablePageBloomFilter",
    "enable_shard_affinity": "enableShardAffinity",
    "memory_snapshot_interval": "memorySnapshotInterval",
    "stable_snapshot_interval": "stableSnapshotInterval",
}

# Allow-list of accepted camelCase form keys for the ``extra`` escape hatch.
# The named parameters already cover the full documented key set; ``extra``
# exists for forward-compatibility if a future server version adds a key. It
# is validated against this allow-list so it cannot be used to POST arbitrary
# keys to /settings/indexes. When Couchbase documents a new GSI setting, add
# its camelCase key here (and, ideally, a named parameter above).
_VALID_GSI_FORM_KEYS: frozenset[str] = frozenset(_GSI_SETTING_KEYS.values())


def admin_index_settings_get(ctx: Context) -> dict[str, Any]:
    """Get the cluster's Global Secondary Index (GSI) settings.

    Reads the GSI settings from the cluster manager (/settings/indexes).
    Returns the settings as a dict, e.g. indexerThreads, logLevel,
    maxRollbackPoints, storageMode, numReplica, redistributeIndexes.
    Read-only.
    """
    settings = get_settings(ctx)
    return get_gsi_settings(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        ca_cert_path=settings.get("ca_cert_path"),
    )


def admin_index_settings_set(
    ctx: Context,
    indexer_threads: int | None = None,
    log_level: str | None = None,
    max_rollback_points: int | None = None,
    storage_mode: str | None = None,
    num_replica: int | None = None,
    redistribute_indexes: bool | None = None,
    enable_page_bloom_filter: bool | None = None,
    enable_shard_affinity: bool | None = None,
    memory_snapshot_interval: int | None = None,
    stable_snapshot_interval: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update the cluster's Global Secondary Index (GSI) settings.

    Provide any subset of the named parameters; only supplied values are
    sent, and unspecified settings are left unchanged by the server. Use
    ``extra`` to pass settings not covered by the named parameters (keys must
    be documented GSI setting camelCase names; unknown keys are rejected).
    Returns the resulting settings after the update.

    Note: Couchbase advises changing most GSI settings only when directed by
    Couchbase Support. This tool loads only when read-only mode is off and
    admin-write mode is on, and (when OAuth is active) requires the write
    scope.
    """
    _require_write_scope()

    named = {
        "indexer_threads": indexer_threads,
        "log_level": log_level,
        "max_rollback_points": max_rollback_points,
        "storage_mode": storage_mode,
        "num_replica": num_replica,
        "redistribute_indexes": redistribute_indexes,
        "enable_page_bloom_filter": enable_page_bloom_filter,
        "enable_shard_affinity": enable_shard_affinity,
        "memory_snapshot_interval": memory_snapshot_interval,
        "stable_snapshot_interval": stable_snapshot_interval,
    }
    params: dict[str, Any] = {
        _GSI_SETTING_KEYS[k]: v for k, v in named.items() if v is not None
    }
    if extra:
        unknown = sorted(set(extra) - _VALID_GSI_FORM_KEYS)
        if unknown:
            raise ValueError(
                f"Unknown GSI setting key(s) in 'extra': {unknown}. Allowed "
                f"keys are {sorted(_VALID_GSI_FORM_KEYS)}. Use a named "
                "parameter where one exists."
            )
        params.update(extra)

    if not params:
        raise ValueError(
            "Provide at least one setting to update (a named parameter or "
            "an 'extra' entry)."
        )

    settings = get_settings(ctx)
    return set_gsi_settings(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        params=params,
        ca_cert_path=settings.get("ca_cert_path"),
    )
