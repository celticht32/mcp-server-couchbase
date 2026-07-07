"""
Tools for scope and collection management.

These tools create, update, drop, and read settings for scopes and
collections within a bucket. They complement the existing read-only
tools ``get_scopes_in_bucket``, ``get_collections_in_scope``, and
``get_scopes_and_collections_in_bucket``.

Write gating
------------
Scope and collection lifecycle is a per-bucket namespace change. These
tools are loaded only when the server is *not* in read-only mode AND
admin-write mode is enabled (see ``tools/__init__.py`` ``get_tools``
and ``ADMIN_WRITE_TOOLS``). When an OAuth token is present, the caller
must additionally hold the write scope; this mirrors the scope
enforcement in ``run_sql_plus_plus_query``.

Drop confirmation
-----------------
``drop_scope`` and ``drop_collection`` require both ``confirm=True`` AND
a ``confirm_name`` that matches the target scope/collection name. This
is a UX guard against fat-finger destruction; combined with
admin-write-mode gating and the write-scope check, three independent
gates precede a drop.

Implementation
--------------
This module uses the Couchbase SDK's ``CollectionManager`` for both
reads and writes so behavior stays consistent with the existing scope
and collection read tools in ``tools/server.py``. TTL and history-
retention settings are represented by the SDK's ``CollectionSpec``
type, which correctly handles the ``max_expiry`` (Couchbase 7.6+) vs.
``max_ttl`` (7.0 - 7.5) transition per the SDK's compatibility layer.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

import logging
from datetime import timedelta
from typing import Any

from couchbase.management.collections import CollectionSpec
from fastmcp import Context
from fastmcp.server.dependencies import get_access_token

from ..utils.connection import connect_to_bucket
from ..utils.constants import MCP_SERVER_NAME, SCOPE_WRITE
from ..utils.context import get_cluster_connection

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.collections_admin")


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
        msg = (
            f"Scope/collection admin requires the '{SCOPE_WRITE}' scope; "
            f"token scopes are {held}."
        )
        logger.warning(msg)
        raise PermissionError(msg)


def _timedelta_or_none(seconds: int | None) -> timedelta | None:
    """Convert an optional integer of seconds to timedelta or return None.

    ``0`` maps to ``timedelta(seconds=0)`` explicitly because Couchbase uses
    it to mean "no TTL" (whereas ``None`` means "leave unchanged" on update).
    """
    if seconds is None:
        return None
    if seconds < 0:
        raise ValueError(f"TTL/max_expiry cannot be negative, got {seconds}")
    return timedelta(seconds=seconds)


# --------------------------------------------------------------------------
# Scope management
# --------------------------------------------------------------------------


def create_scope(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
) -> dict[str, Any]:
    """Create a scope within a bucket.

    Both ``bucket_name`` and ``scope_name`` are required. Returns a dict
    with the created bucket and scope names.

    Fails with ``ScopeAlreadyExistsException`` (from the SDK) if a scope
    with that name already exists in the bucket.
    """
    _require_write_scope()

    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.info(f"Creating scope {scope_name!r} in bucket {bucket_name!r}")
        bucket.collections().create_scope(scope_name)
        return {"bucket": bucket_name, "scope": scope_name, "created": True}
    except Exception as e:
        logger.error(
            f"Error creating scope {scope_name!r} in bucket {bucket_name!r}: {e}",
            exc_info=True,
        )
        raise


def drop_scope(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    confirm: bool = False,
    confirm_name: str | None = None,
) -> dict[str, Any]:
    """Drop a scope from a bucket. Irreversible; deletes all collections
    (and thus all documents) in the scope.

    Requires BOTH:

    - ``confirm=True``
    - ``confirm_name`` set to the exact scope name

    The name-match is a UX guard against fat-finger deletion; combined
    with the admin-write-mode gate and the OAuth write-scope check,
    three independent gates precede a drop.

    Cannot drop ``_default`` (Couchbase server rejects it).
    """
    _require_write_scope()

    if not confirm:
        raise ValueError(
            "drop_scope requires confirm=True. This operation is "
            "irreversible and deletes every collection and document in "
            "the scope."
        )
    if confirm_name != scope_name:
        raise ValueError(
            "drop_scope requires confirm_name to exactly match "
            f"scope_name ({scope_name!r}). This guard against fat-finger "
            "deletion matches the delete_bucket pattern."
        )
    if scope_name == "_default":
        raise ValueError(
            "The _default scope cannot be dropped (Couchbase server "
            "rejects this operation)."
        )

    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.warning(f"Dropping scope {scope_name!r} in bucket {bucket_name!r}")
        bucket.collections().drop_scope(scope_name)
        return {"bucket": bucket_name, "scope": scope_name, "dropped": True}
    except Exception as e:
        logger.error(
            f"Error dropping scope {scope_name!r} in bucket {bucket_name!r}: {e}",
            exc_info=True,
        )
        raise


# --------------------------------------------------------------------------
# Collection management
# --------------------------------------------------------------------------


def create_collection(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    max_expiry_seconds: int | None = None,
    history: bool | None = None,
) -> dict[str, Any]:
    """Create a collection inside a scope.

    Required: ``bucket_name``, ``scope_name``, ``collection_name``.

    Optional per-collection settings:

    - ``max_expiry_seconds``: default document TTL in seconds. ``0`` = no
      TTL (documents don't expire). ``None`` = inherit from the bucket
      default.
    - ``history``: enable per-document history retention (Couchbase 7.2+
      with Magma storage engine). ``None`` = server default.

    Returns a dict with the created keyspace and settings applied.
    """
    _require_write_scope()

    spec = CollectionSpec(
        collection_name=collection_name,
        scope_name=scope_name,
        max_expiry=_timedelta_or_none(max_expiry_seconds),
        history=history,
    )

    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.info(f"Creating collection {bucket_name}.{scope_name}.{collection_name}")
        bucket.collections().create_collection(spec)
        return {
            "bucket": bucket_name,
            "scope": scope_name,
            "collection": collection_name,
            "max_expiry_seconds": max_expiry_seconds,
            "history": history,
            "created": True,
        }
    except Exception as e:
        logger.error(
            f"Error creating collection "
            f"{bucket_name}.{scope_name}.{collection_name}: {e}",
            exc_info=True,
        )
        raise


def drop_collection(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    confirm: bool = False,
    confirm_name: str | None = None,
) -> dict[str, Any]:
    """Drop a collection. Irreversible; deletes all documents in the
    collection.

    Requires BOTH:

    - ``confirm=True``
    - ``confirm_name`` set to the exact collection name

    The name-match is a UX guard against fat-finger deletion; combined
    with the admin-write-mode gate and the OAuth write-scope check,
    three independent gates precede a drop.

    Cannot drop ``_default`` collection from ``_default`` scope (Couchbase
    server rejects it).
    """
    _require_write_scope()

    if not confirm:
        raise ValueError(
            "drop_collection requires confirm=True. This operation is "
            "irreversible and deletes every document in the collection."
        )
    if confirm_name != collection_name:
        raise ValueError(
            "drop_collection requires confirm_name to exactly match "
            f"collection_name ({collection_name!r}). This guard against "
            "fat-finger deletion matches the delete_bucket pattern."
        )
    if scope_name == "_default" and collection_name == "_default":
        raise ValueError(
            "The _default collection in the _default scope cannot be "
            "dropped (Couchbase server rejects this operation)."
        )

    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.warning(
            f"Dropping collection {bucket_name}.{scope_name}.{collection_name}"
        )
        # The SDK's drop_collection takes a CollectionSpec but only reads
        # scope_name and collection_name from it, so a minimal spec is fine.
        spec = CollectionSpec(collection_name=collection_name, scope_name=scope_name)
        bucket.collections().drop_collection(spec)
        return {
            "bucket": bucket_name,
            "scope": scope_name,
            "collection": collection_name,
            "dropped": True,
        }
    except Exception as e:
        logger.error(
            f"Error dropping collection "
            f"{bucket_name}.{scope_name}.{collection_name}: {e}",
            exc_info=True,
        )
        raise


def update_collection(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    max_expiry_seconds: int | None = None,
    history: bool | None = None,
) -> dict[str, Any]:
    """Update mutable settings on an existing collection.

    Both ``max_expiry_seconds`` and ``history`` are optional; only the
    values explicitly provided are sent to the server. Passing both as
    ``None`` (the default) is a no-op — raises an error since the caller
    likely made a mistake.

    Returns a dict with the settings sent to the server.
    """
    _require_write_scope()

    if max_expiry_seconds is None and history is None:
        raise ValueError(
            "update_collection requires at least one of "
            "max_expiry_seconds or history to be provided."
        )

    spec = CollectionSpec(
        collection_name=collection_name,
        scope_name=scope_name,
        max_expiry=_timedelta_or_none(max_expiry_seconds),
        history=history,
    )

    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.info(f"Updating collection {bucket_name}.{scope_name}.{collection_name}")
        bucket.collections().update_collection(spec)
        return {
            "bucket": bucket_name,
            "scope": scope_name,
            "collection": collection_name,
            "max_expiry_seconds": max_expiry_seconds,
            "history": history,
            "updated": True,
        }
    except Exception as e:
        logger.error(
            f"Error updating collection "
            f"{bucket_name}.{scope_name}.{collection_name}: {e}",
            exc_info=True,
        )
        raise


# --------------------------------------------------------------------------
# Collection settings read
# --------------------------------------------------------------------------


def get_collection_settings(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
) -> dict[str, Any]:
    """Get the settings for a single collection.

    Returns a dict with:

    - ``bucket``, ``scope``, ``collection``: the keyspace identifiers
    - ``max_expiry_seconds``: default TTL in seconds; ``0`` = no TTL,
      ``-1`` = inherit bucket default (per Couchbase 7.6+ semantics),
      ``None`` = server did not return a value
    - ``history``: whether per-document history retention is enabled

    Raises ``ValueError`` if the requested scope or collection does not
    exist in the bucket.
    """
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(
            f"Reading settings for {bucket_name}.{scope_name}.{collection_name}"
        )
        scopes = bucket.collections().get_all_scopes()
        for scope in scopes:
            if scope.name != scope_name:
                continue
            for coll in scope.collections:
                if coll.name != collection_name:
                    continue
                # SDK exposes max_expiry as timedelta or None
                exp = getattr(coll, "max_expiry", None)
                if isinstance(exp, timedelta):
                    max_expiry_seconds = int(exp.total_seconds())
                else:
                    max_expiry_seconds = None
                return {
                    "bucket": bucket_name,
                    "scope": scope_name,
                    "collection": collection_name,
                    "max_expiry_seconds": max_expiry_seconds,
                    "history": getattr(coll, "history", None),
                }
            raise ValueError(
                f"Collection {collection_name!r} not found in scope "
                f"{scope_name!r} of bucket {bucket_name!r}"
            )
        raise ValueError(f"Scope {scope_name!r} not found in bucket {bucket_name!r}")
    except ValueError:
        raise
    except Exception as e:
        logger.error(
            f"Error reading settings for "
            f"{bucket_name}.{scope_name}.{collection_name}: {e}",
            exc_info=True,
        )
        raise
