"""
Tools for bucket management.

These tools create, update, delete, flush, compact, and load-sample buckets
via the Couchbase Cluster Manager Management REST API
(``/pools/default/buckets`` and ``/sampleBuckets/install``). They complement
the read-only bucket-discovery tools ``get_buckets_in_cluster``,
``get_scopes_in_bucket``, and ``get_scopes_and_collections_in_bucket``.

Write gating
------------
Bucket lifecycle mutates cluster structure. These tools are loaded only when
the server is *not* in read-only mode AND admin-write mode is enabled (see
``tools/__init__.py`` ``get_tools`` and ``ADMIN_WRITE_TOOLS``). When an
OAuth token is present, the caller must additionally hold the write scope;
this mirrors the scope enforcement in ``run_sql_plus_plus_query`` so a
read-scoped token cannot mutate buckets even when admin-write mode is on.

REST vs. SDK
------------
Bucket lifecycle goes through the Cluster Manager REST endpoints, not the
Couchbase SDK's Bucket Manager, so behavior stays consistent with the
existing REST path used by ``list_indexes`` and ``admin_index_settings_*``
(host extraction, TLS handling, and Capella root CA are all reused from
``utils.index_utils``).

Delete confirmation
-------------------
``delete_bucket`` requires both ``confirm=True`` AND a ``confirm_name`` that
matches the target bucket name. This mirrors the Couchbase Web Console
"type the bucket name to delete" pattern and is a UX guard against
fat-finger destruction; combined with admin-write-mode gating and the
write-scope check, three independent gates precede a bucket delete.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

import logging
from typing import Any

from fastmcp import Context
from fastmcp.server.dependencies import get_access_token

from ..utils.bucket_admin_rest import (
    ALLOWED_COMPACT_ACTIONS,
    ALLOWED_SAMPLE_BUCKETS,
    assert_bucket_name,
    compact_bucket_rest,
    create_bucket_rest,
    delete_bucket_rest,
    flush_bucket_rest,
    load_sample_bucket_rest,
    update_bucket_rest,
    validate_extra_bucket_keys,
)
from ..utils.config import get_settings
from ..utils.constants import MCP_SERVER_NAME, SCOPE_WRITE

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.bucket_admin")


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
            f"Bucket admin requires the '{SCOPE_WRITE}' scope; token scopes are {held}."
        )
        logger.warning(msg)
        raise PermissionError(msg)


# --------------------------------------------------------------------------
# Bucket parameter mapping (POST /pools/default/buckets accepts snake_case
# for some keys and camelCase for others; we normalize to the endpoint's
# actual key names)
# --------------------------------------------------------------------------

# Named bucket parameters exposed as tool arguments. Maps tool argument name
# to the REST endpoint's form key. Verified against Couchbase Server docs
# (rest-api/rest-bucket-create, rest-bucket-update) 2026-07-06.
_BUCKET_PARAM_KEYS: dict[str, str] = {
    "ram_quota_mb": "ramQuota",
    "bucket_type": "bucketType",  # membase (couchbase) | ephemeral | memcached
    "storage_backend": "storageBackend",  # couchstore | magma
    "replica_number": "replicaNumber",
    "replica_index": "replicaIndex",
    "eviction_policy": "evictionPolicy",  # valueOnly | fullEviction | noEviction | nruEviction
    "flush_enabled": "flushEnabled",  # 0 | 1 in the REST form
    "max_ttl": "maxTTL",
    "compression_mode": "compressionMode",  # off | passive | active
    "conflict_resolution_type": "conflictResolutionType",  # seqno | lww | custom
    "durability_min_level": "durabilityMinLevel",  # none | majority | majorityAndPersistActive | persistToMajority
    "num_vbuckets": "numVBuckets",
    "history_retention_seconds": "historyRetentionSeconds",
    "history_retention_bytes": "historyRetentionBytes",
    "history_retention_collection_default": "historyRetentionCollectionDefault",
    "autocompaction_defined": "autoCompactionDefined",
}

# Allow-list of accepted camelCase form keys for the ``extra`` escape hatch.
# The named parameters cover the documented parameter set; ``extra`` exists
# for forward-compatibility. Any key not in this set is rejected before it
# reaches the REST endpoint. When Couchbase documents a new bucket setting,
# add its camelCase key here (and, ideally, a named parameter above).
_VALID_BUCKET_FORM_KEYS: frozenset[str] = frozenset(_BUCKET_PARAM_KEYS.values())


def _collect_bucket_params(
    named: dict[str, Any], extra: dict[str, Any] | None
) -> dict[str, Any]:
    """Merge named args and validated extras into a REST form dict."""
    params: dict[str, Any] = {
        _BUCKET_PARAM_KEYS[k]: v for k, v in named.items() if v is not None
    }
    if extra:
        validate_extra_bucket_keys(extra, _VALID_BUCKET_FORM_KEYS)
        params.update(extra)
    return params


# --------------------------------------------------------------------------
# Tool functions
# --------------------------------------------------------------------------


def create_bucket(
    ctx: Context,
    bucket_name: str | None = None,
    body: dict[str, Any] | None = None,
    ram_quota_mb: int | None = None,
    bucket_type: str | None = None,
    storage_backend: str | None = None,
    replica_number: int | None = None,
    replica_index: int | None = None,
    eviction_policy: str | None = None,
    flush_enabled: bool | None = None,
    max_ttl: int | None = None,
    compression_mode: str | None = None,
    conflict_resolution_type: str | None = None,
    durability_min_level: str | None = None,
    num_vbuckets: int | None = None,
    history_retention_seconds: int | None = None,
    history_retention_bytes: int | None = None,
    history_retention_collection_default: bool | None = None,
    autocompaction_defined: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a bucket on the cluster.

    Two ways to call this:

    - Raw ``body``: pass a dict of Couchbase REST form keys (camelCase).
      Every key is validated against the documented bucket-parameter
      allow-list; unknown keys are rejected. ``body["name"]`` must be
      present and match ``bucket_name`` if both are provided.
    - Structured fields: pass ``bucket_name`` (required) and ``ram_quota_mb``
      (required), plus any subset of the optional named parameters (or
      ``extra`` for forward-compatibility settings). Only supplied values
      are sent, matching the endpoint's leave-unspecified-alone semantics
      on update but combined with server defaults on create.

    Returns a dict with the executed body and the cluster's response.
    """
    _require_write_scope()

    if body is not None:
        if bucket_name and body.get("name") and body["name"] != bucket_name:
            raise ValueError(
                f"bucket_name={bucket_name!r} does not match body['name']={body['name']!r}"
            )
        name = body.get("name") or bucket_name
        if not name:
            raise ValueError("body must include a 'name' key or provide bucket_name")
        assert_bucket_name(name)
        # Validate ALL body keys, since we're accepting a raw form
        validate_extra_bucket_keys(
            {k: v for k, v in body.items() if k != "name"},
            _VALID_BUCKET_FORM_KEYS,
        )
        form = dict(body)
        form["name"] = name
    else:
        if not bucket_name:
            raise ValueError("bucket_name is required when body is not provided")
        assert_bucket_name(bucket_name)
        if ram_quota_mb is None:
            raise ValueError(
                "ram_quota_mb is required when body is not provided (Couchbase "
                "requires an explicit RAM quota for new buckets)"
            )
        named = {
            "ram_quota_mb": ram_quota_mb,
            "bucket_type": bucket_type,
            "storage_backend": storage_backend,
            "replica_number": replica_number,
            "replica_index": replica_index,
            "eviction_policy": eviction_policy,
            "flush_enabled": flush_enabled,
            "max_ttl": max_ttl,
            "compression_mode": compression_mode,
            "conflict_resolution_type": conflict_resolution_type,
            "durability_min_level": durability_min_level,
            "num_vbuckets": num_vbuckets,
            "history_retention_seconds": history_retention_seconds,
            "history_retention_bytes": history_retention_bytes,
            "history_retention_collection_default": history_retention_collection_default,
            "autocompaction_defined": autocompaction_defined,
        }
        form = {"name": bucket_name}
        form.update(_collect_bucket_params(named, extra))

    logger.info(f"Creating bucket {form['name']!r}")
    settings = get_settings(ctx)
    result = create_bucket_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        form=form,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"body": form, "result": result}


def update_bucket(
    ctx: Context,
    bucket_name: str,
    body: dict[str, Any] | None = None,
    ram_quota_mb: int | None = None,
    replica_number: int | None = None,
    replica_index: int | None = None,
    eviction_policy: str | None = None,
    flush_enabled: bool | None = None,
    max_ttl: int | None = None,
    compression_mode: str | None = None,
    durability_min_level: str | None = None,
    history_retention_seconds: int | None = None,
    history_retention_bytes: int | None = None,
    history_retention_collection_default: bool | None = None,
    autocompaction_defined: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update an existing bucket's settings.

    Two ways to call this:

    - Raw ``body``: dict of Couchbase REST form keys (camelCase). Validated
      against the same allow-list ``create_bucket`` uses.
    - Structured fields: pass any subset of the optional named parameters
      (or ``extra`` for forward-compatibility). Only supplied values are
      sent; unspecified settings are left unchanged by the server.

    Note: ``bucket_type``, ``storage_backend``, ``conflict_resolution_type``,
    and ``num_vbuckets`` are set at create time and cannot be updated; they
    are intentionally omitted from the named parameters here. Attempting to
    change them via ``body`` or ``extra`` will produce a server-side error.

    Returns a dict with the executed body and the cluster's response.
    """
    _require_write_scope()
    assert_bucket_name(bucket_name)

    if body is not None:
        validate_extra_bucket_keys(body, _VALID_BUCKET_FORM_KEYS)
        form = dict(body)
    else:
        named = {
            "ram_quota_mb": ram_quota_mb,
            "replica_number": replica_number,
            "replica_index": replica_index,
            "eviction_policy": eviction_policy,
            "flush_enabled": flush_enabled,
            "max_ttl": max_ttl,
            "compression_mode": compression_mode,
            "durability_min_level": durability_min_level,
            "history_retention_seconds": history_retention_seconds,
            "history_retention_bytes": history_retention_bytes,
            "history_retention_collection_default": history_retention_collection_default,
            "autocompaction_defined": autocompaction_defined,
        }
        form = _collect_bucket_params(named, extra)

    if not form:
        raise ValueError(
            "Provide at least one field to update (a named parameter, "
            "'body', or an 'extra' entry)."
        )

    logger.info(f"Updating bucket {bucket_name!r}")
    settings = get_settings(ctx)
    result = update_bucket_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        bucket_name=bucket_name,
        form=form,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"bucket": bucket_name, "body": form, "result": result}


def delete_bucket(
    ctx: Context,
    bucket_name: str,
    confirm: bool = False,
    confirm_name: str | None = None,
) -> dict[str, Any]:
    """Delete a bucket. Irreversible; deletes all data in the bucket.

    Requires BOTH:

    - ``confirm=True``
    - ``confirm_name`` set to the exact bucket name

    The name-match is a UX guard against fat-finger deletes, matching the
    Couchbase Web Console pattern. Combined with the admin-write-mode gate
    and the OAuth write-scope check (when applicable), three independent
    gates precede a delete.

    Returns a dict with the deleted bucket name and the cluster response.
    """
    _require_write_scope()
    assert_bucket_name(bucket_name)

    if not confirm:
        raise ValueError(
            "delete_bucket requires confirm=True. This operation is "
            "irreversible and deletes all data in the bucket."
        )
    if confirm_name != bucket_name:
        raise ValueError(
            "delete_bucket requires confirm_name to exactly match "
            f"bucket_name ({bucket_name!r}). This guard against fat-finger "
            "deletion matches the Couchbase Web Console pattern."
        )

    logger.warning(f"Deleting bucket {bucket_name!r}")
    settings = get_settings(ctx)
    result = delete_bucket_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        bucket_name=bucket_name,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"bucket": bucket_name, "deleted": True, "result": result}


def flush_bucket(
    ctx: Context,
    bucket_name: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Flush a bucket, deleting all documents but keeping the bucket itself.

    The bucket must have been created with ``flush_enabled=True`` (via
    ``create_bucket``) or updated to allow flush before this call succeeds;
    Couchbase returns 400 otherwise.

    Requires ``confirm=True``. Combined with the admin-write-mode gate and
    the write-scope check, three independent gates precede a flush.

    Returns a dict with the flushed bucket name and the cluster response.
    """
    _require_write_scope()
    assert_bucket_name(bucket_name)

    if not confirm:
        raise ValueError(
            "flush_bucket requires confirm=True. This operation deletes "
            "every document in the bucket."
        )

    logger.warning(f"Flushing bucket {bucket_name!r}")
    settings = get_settings(ctx)
    result = flush_bucket_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        bucket_name=bucket_name,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"bucket": bucket_name, "flushed": True, "result": result}


def compact_bucket(
    ctx: Context,
    bucket_name: str,
    action: str = "start",
) -> dict[str, Any]:
    """Start or cancel a bucket compaction task.

    ``action`` is either ``"start"`` (POST ``.../controller/compactBucket``)
    or ``"cancel"`` (POST ``.../controller/cancelBucketCompaction``). Not
    destructive of data; compaction reclaims disk space. Cancel is idempotent
    if no task is running.

    Returns a dict with the bucket, action, and the cluster response.
    """
    _require_write_scope()
    assert_bucket_name(bucket_name)

    if action not in ALLOWED_COMPACT_ACTIONS:
        raise ValueError(
            f"action must be one of {sorted(ALLOWED_COMPACT_ACTIONS)}, got {action!r}"
        )

    logger.info(f"Compact ({action}) bucket {bucket_name!r}")
    settings = get_settings(ctx)
    result = compact_bucket_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        bucket_name=bucket_name,
        action=action,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"bucket": bucket_name, "action": action, "result": result}


def load_sample_bucket(
    ctx: Context,
    sample_name: str,
) -> dict[str, Any]:
    """Install one of Couchbase's built-in sample datasets.

    ``sample_name`` must be one of the documented sample bucket names
    (``travel-sample``, ``beer-sample``, ``gamesim-sample``). Any other
    value is rejected. Installation is asynchronous on the server side;
    the initial response indicates the task was accepted.

    Returns a dict with the sample name and the cluster response.
    """
    _require_write_scope()

    if sample_name not in ALLOWED_SAMPLE_BUCKETS:
        raise ValueError(
            f"sample_name must be one of {sorted(ALLOWED_SAMPLE_BUCKETS)}, "
            f"got {sample_name!r}"
        )

    logger.info(f"Loading sample bucket {sample_name!r}")
    settings = get_settings(ctx)
    result = load_sample_bucket_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        sample_name=sample_name,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"sample": sample_name, "result": result}
