"""
Tools for FTS (Full-Text Search) index administration.

FTS is Couchbase's built-in search service. This module exposes ten tools
covering the index lifecycle:

- **CRUD**: create, get, list, update, delete
- **Control**: pause/resume ingestion, allow/disallow queries
- **Diagnostics**: get document count, analyze a document against a mapping

Write gating
------------
Index CRUD and control mutate the FTS service's persistent state. Write
tools are loaded only when the server is *not* in read-only mode AND
admin-write mode is enabled (see ``tools/__init__.py`` ``get_tools`` and
``ADMIN_WRITE_TOOLS``). When an OAuth token is present, the caller must
additionally hold the write scope; this mirrors the scope enforcement in
the other admin modules.

Delete confirmation
-------------------
``delete_fts_index`` requires BOTH ``confirm=True`` AND a ``confirm_name``
matching the target index name. This mirrors the pattern established by
``delete_bucket`` and ``delete_remote_cluster``; combined with the
admin-write-mode gate and the OAuth write-scope check, three independent
gates precede a destructive index operation.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

import logging
from typing import Any

from fastmcp import Context
from fastmcp.server.dependencies import get_access_token

from ..utils.config import get_settings
from ..utils.constants import MCP_SERVER_NAME, SCOPE_WRITE
from ..utils.fts_rest import (
    analyze_doc_rest,
    assert_fts_index_name,
    create_or_update_fts_index_rest,
    delete_fts_index_rest,
    get_fts_index_count_rest,
    get_fts_index_rest,
    ingest_control_rest,
    list_fts_indexes_rest,
    query_control_rest,
)

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.fts_admin")


def _require_write_scope() -> None:
    """Raise ``PermissionError`` if a token is present but lacks the write scope."""
    token = get_access_token()
    if token is not None and SCOPE_WRITE not in (token.scopes or []):
        held = sorted(set(token.scopes or []))
        msg = f"FTS admin requires the '{SCOPE_WRITE}' scope; token scopes are {held}."
        logger.warning(msg)
        raise PermissionError(msg)


# --------------------------------------------------------------------------
# Read tools (READ_ONLY_TOOLS)
# --------------------------------------------------------------------------


def list_fts_indexes(ctx: Context) -> dict[str, Any]:
    """List all FTS indexes on the cluster.

    Returns the raw response from the FTS service, which is a dict with an
    ``indexDefs`` key containing per-index definitions.
    """
    settings = get_settings(ctx)
    return list_fts_indexes_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        ca_cert_path=settings.get("ca_cert_path"),
    )


def get_fts_index(
    ctx: Context,
    index_name: str,
) -> dict[str, Any]:
    """Get the full definition of an FTS index (mapping, source config,
    plan params). Read-only.
    """
    assert_fts_index_name(index_name)
    settings = get_settings(ctx)
    return get_fts_index_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        index_name=index_name,
        ca_cert_path=settings.get("ca_cert_path"),
    )


def get_fts_index_count(
    ctx: Context,
    index_name: str,
) -> dict[str, Any]:
    """Get the document count for an FTS index. Read-only.

    Returns a dict with a ``count`` key; useful for confirming an index has
    fully ingested its source data before running queries against it.
    """
    assert_fts_index_name(index_name)
    settings = get_settings(ctx)
    return get_fts_index_count_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        index_name=index_name,
        ca_cert_path=settings.get("ca_cert_path"),
    )


def analyze_document(
    ctx: Context,
    index_name: str,
    doc: dict[str, Any],
) -> dict[str, Any]:
    """Test how a document would be indexed against an FTS index.

    Sends the ``doc`` JSON to ``/api/analyzeDoc/{index_name}`` and returns
    the FTS service's analyzer output (tokenization, byte offsets, field
    routing). No document is stored; this is a diagnostic tool for tuning
    index mappings and analyzer configuration.

    Read-only in that it doesn't mutate the index. Loaded in the read-only
    category so users can verify index behavior without needing
    admin-write-mode.
    """
    assert_fts_index_name(index_name)
    if not isinstance(doc, dict):
        raise ValueError(f"doc must be a dict (JSON object), got {type(doc).__name__}")
    settings = get_settings(ctx)
    return analyze_doc_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        index_name=index_name,
        doc=doc,
        ca_cert_path=settings.get("ca_cert_path"),
    )


# --------------------------------------------------------------------------
# Write tools (ADMIN_WRITE_TOOLS)
# --------------------------------------------------------------------------


def create_fts_index(
    ctx: Context,
    index_name: str,
    definition: dict[str, Any],
) -> dict[str, Any]:
    """Create a new FTS index.

    ``definition`` is a JSON object matching Couchbase's FTS index-definition
    schema: ``type``, ``name``, ``sourceType``, ``sourceName``, ``params``
    (with ``mapping``, ``store``, ``doc_config``), ``planParams``. The
    ``name`` field inside ``definition`` should match ``index_name``; if
    it doesn't, the tool sets it to ``index_name`` for consistency.

    Fails with a 400 (surfaced as ``RuntimeError``) if the index already
    exists — use ``update_fts_index`` to modify an existing one.

    Returns a dict with the executed definition and the FTS service response.
    """
    _require_write_scope()
    assert_fts_index_name(index_name)

    if not isinstance(definition, dict):
        raise ValueError(f"definition must be a dict, got {type(definition).__name__}")
    # Normalize: force the definition's name to match the URL segment.
    body = dict(definition)
    body["name"] = index_name

    logger.info(f"Creating FTS index {index_name!r}")
    settings = get_settings(ctx)
    result = create_or_update_fts_index_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        index_name=index_name,
        definition=body,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"index_name": index_name, "definition": body, "result": result}


def update_fts_index(
    ctx: Context,
    index_name: str,
    definition: dict[str, Any],
) -> dict[str, Any]:
    """Update an existing FTS index.

    Uses the same PUT endpoint as ``create_fts_index``; the server accepts
    updates on existing indexes and applies them in place. If the index
    does not exist, the server treats this as a create.

    Callers who want strict update-only semantics can call
    ``get_fts_index`` first to verify existence; enforcing it client-side
    would race the server anyway.
    """
    _require_write_scope()
    assert_fts_index_name(index_name)

    if not isinstance(definition, dict):
        raise ValueError(f"definition must be a dict, got {type(definition).__name__}")
    body = dict(definition)
    body["name"] = index_name

    logger.info(f"Updating FTS index {index_name!r}")
    settings = get_settings(ctx)
    result = create_or_update_fts_index_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        index_name=index_name,
        definition=body,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"index_name": index_name, "definition": body, "result": result}


def delete_fts_index(
    ctx: Context,
    index_name: str,
    confirm: bool = False,
    confirm_name: str | None = None,
) -> dict[str, Any]:
    """Delete an FTS index. Irreversible; drops the index and all its
    persisted data.

    Requires BOTH:

    - ``confirm=True``
    - ``confirm_name`` set to the exact index name

    The name-match is a UX guard against fat-finger deletion; combined
    with the admin-write-mode gate and the OAuth write-scope check,
    three independent gates precede a delete.
    """
    _require_write_scope()
    assert_fts_index_name(index_name)

    if not confirm:
        raise ValueError(
            "delete_fts_index requires confirm=True. This drops the index "
            "and its persisted data; re-indexing requires a full ingest."
        )
    if confirm_name != index_name:
        raise ValueError(
            "delete_fts_index requires confirm_name to exactly match "
            f"index_name ({index_name!r}). This guard against fat-finger "
            "deletion matches the delete_bucket pattern."
        )

    logger.warning(f"Deleting FTS index {index_name!r}")
    settings = get_settings(ctx)
    result = delete_fts_index_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        index_name=index_name,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"index_name": index_name, "deleted": True, "result": result}


def pause_fts_index_ingest(
    ctx: Context,
    index_name: str,
) -> dict[str, Any]:
    """Pause ingestion for an FTS index.

    Stops the index from ingesting new source-data mutations. Queries
    continue to work against previously-ingested data. Reversible via
    ``resume_fts_index_ingest``.
    """
    _require_write_scope()
    assert_fts_index_name(index_name)

    logger.info(f"Pausing ingest on FTS index {index_name!r}")
    settings = get_settings(ctx)
    result = ingest_control_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        index_name=index_name,
        op="pause",
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"index_name": index_name, "ingest_paused": True, "result": result}


def resume_fts_index_ingest(
    ctx: Context,
    index_name: str,
) -> dict[str, Any]:
    """Resume ingestion for a previously-paused FTS index."""
    _require_write_scope()
    assert_fts_index_name(index_name)

    logger.info(f"Resuming ingest on FTS index {index_name!r}")
    settings = get_settings(ctx)
    result = ingest_control_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        index_name=index_name,
        op="resume",
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"index_name": index_name, "ingest_resumed": True, "result": result}


def set_fts_index_query_control(
    ctx: Context,
    index_name: str,
    allow: bool,
) -> dict[str, Any]:
    """Allow or disallow queries against an FTS index.

    When ``allow=True`` the index accepts queries; when ``False`` the FTS
    service rejects queries against the index (useful during maintenance
    windows or when data is being reloaded and query results would be
    inconsistent).

    Independent of ingest control: an index can be ingesting but not
    accepting queries, or vice versa.
    """
    _require_write_scope()
    assert_fts_index_name(index_name)

    op = "allow" if allow else "disallow"
    logger.info(f"Setting queryControl={op} on FTS index {index_name!r}")
    settings = get_settings(ctx)
    result = query_control_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        index_name=index_name,
        op=op,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"index_name": index_name, "queries_allowed": allow, "result": result}
