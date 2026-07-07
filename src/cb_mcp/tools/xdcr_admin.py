"""
Tools for XDCR (Cross-Datacenter Replication) administration.

XDCR is split between two concepts:

- **Remote clusters**: named references to peer Couchbase clusters
  (create/list/update/delete). A remote-cluster reference is a stored
  hostname + credentials pair.
- **Replications**: bucket-to-bucket (or bucket-to-collection) streams
  that use a remote cluster as a target. Replications have lifecycle
  (create, pause, resume, delete) and per-replication settings
  (throttling, filter expressions, priority, compression).

Write gating
------------
XDCR admin mutates cross-cluster infrastructure. These tools are loaded
only when the server is *not* in read-only mode AND admin-write mode is
enabled (see ``tools/__init__.py`` ``get_tools`` and
``ADMIN_WRITE_TOOLS``). When an OAuth token is present, the caller must
additionally hold the write scope; this mirrors the scope enforcement in
``index_admin.py``, ``bucket_admin.py``, and ``collections_admin.py``.

Delete confirmation
-------------------
``delete_remote_cluster`` and ``delete_replication`` both require BOTH
``confirm=True`` AND a ``confirm_name`` matching the target's identifier.
This mirrors the pattern established by ``delete_bucket`` in the bucket
admin module; combined with admin-write-mode gating and the write-scope
check, three independent gates precede a destructive XDCR operation.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

import logging
from typing import Any

from fastmcp import Context
from fastmcp.server.dependencies import get_access_token

from ..utils.config import get_settings
from ..utils.constants import MCP_SERVER_NAME, SCOPE_WRITE
from ..utils.xdcr_rest import (
    assert_remote_cluster_name,
    assert_replication_id,
    create_remote_cluster_rest,
    create_replication_rest,
    delete_remote_cluster_rest,
    delete_replication_rest,
    get_replication_settings_rest,
    list_remote_clusters_rest,
    list_replications_rest,
    update_remote_cluster_rest,
    update_replication_settings_rest,
)

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.xdcr_admin")


def _require_write_scope() -> None:
    """Raise ``PermissionError`` if a token is present but lacks the write scope."""
    token = get_access_token()
    if token is not None and SCOPE_WRITE not in (token.scopes or []):
        held = sorted(set(token.scopes or []))
        msg = f"XDCR admin requires the '{SCOPE_WRITE}' scope; token scopes are {held}."
        logger.warning(msg)
        raise PermissionError(msg)


# --------------------------------------------------------------------------
# Parameter key maps (snake_case → camelCase form keys per Couchbase docs)
# --------------------------------------------------------------------------

_REMOTE_CLUSTER_KEYS: dict[str, str] = {
    "hostname": "hostname",
    "username": "username",
    "password": "password",
    "demand_encryption": "demandEncryption",
    "encryption_type": "encryptionType",  # full | half (deprecated) | none
    "certificate": "certificate",
    "client_certificate": "clientCertificate",
    "client_key": "clientKey",
    "network_type": "network_type",
}
_VALID_REMOTE_CLUSTER_KEYS: frozenset[str] = frozenset(_REMOTE_CLUSTER_KEYS.values())


# createReplication accepts a different key set than settings-update.
# Both share ``pauseRequested`` and the "delivery" tuning keys.
_CREATE_REPLICATION_KEYS: dict[str, str] = {
    "from_bucket": "fromBucket",
    "to_cluster": "toCluster",
    "to_bucket": "toBucket",
    "replication_type": "replicationType",  # continuous | one-time
    "filter_expression": "filterExpression",
    "priority": "priority",  # High | Medium | Low
    "compression_type": "compressionType",  # None | Auto | Snappy
    "network_type": "networkType",
}
_VALID_CREATE_REPLICATION_KEYS: frozenset[str] = frozenset(
    _CREATE_REPLICATION_KEYS.values()
)


_REPLICATION_SETTINGS_KEYS: dict[str, str] = {
    "pause_requested": "pauseRequested",
    "priority": "priority",
    "compression_type": "compressionType",
    "filter_expression": "filterExpression",
    "filter_skip_restream": "filterSkipRestream",
    "checkpoint_interval": "checkpointInterval",
    "worker_batch_size": "workerBatchSize",
    "doc_batch_size_kb": "docBatchSizeKb",
    "failure_restart_interval": "failureRestartInterval",
    "source_nozzle_per_node": "sourceNozzlePerNode",
    "target_nozzle_per_node": "targetNozzlePerNode",
    "log_level": "logLevel",
    "stats_interval": "statsInterval",
    "network_type": "networkType",
    "collections_mapping_rules": "collectionsMappingRules",  # JSON
    "collections_migration_mode": "collectionsMigrationMode",
    "collections_mirroring_mode": "collectionsMirroringMode",
    "collections_explicit_mapping": "collectionsExplicitMapping",
    "collections_oso_mode": "collectionsOSOMode",
}
_VALID_REPLICATION_SETTINGS_KEYS: frozenset[str] = frozenset(
    _REPLICATION_SETTINGS_KEYS.values()
)


def _collect(
    keymap: dict[str, str],
    allowed: frozenset[str],
    named: dict[str, Any],
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge named args and extras into a form-body dict.

    Rejects unknown ``extra`` keys against ``allowed``.
    """
    form: dict[str, Any] = {keymap[k]: v for k, v in named.items() if v is not None}
    if extra:
        unknown = sorted(set(extra) - allowed)
        if unknown:
            raise ValueError(
                f"Unknown XDCR setting key(s) in 'extra': {unknown}. "
                f"Allowed keys are {sorted(allowed)}. Use a named parameter "
                "where one exists."
            )
        form.update(extra)
    return form


# --------------------------------------------------------------------------
# Remote-cluster tools
# --------------------------------------------------------------------------


def create_remote_cluster(
    ctx: Context,
    name: str,
    hostname: str,
    username: str,
    password: str,
    demand_encryption: bool | None = None,
    encryption_type: str | None = None,
    certificate: str | None = None,
    client_certificate: str | None = None,
    client_key: str | None = None,
    network_type: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a remote-cluster reference.

    Required: ``name`` (unique reference name), ``hostname`` (target
    cluster's connection string, e.g. ``couchbases://target.example.com``),
    ``username`` and ``password`` (credentials on the target cluster).

    Optional TLS-related fields (``demand_encryption``, ``encryption_type``,
    ``certificate``, ``client_certificate``, ``client_key``) are required
    when the target cluster requires TLS.

    Returns a dict with the form body sent and the cluster response.
    """
    _require_write_scope()
    assert_remote_cluster_name(name)

    named = {
        "hostname": hostname,
        "username": username,
        "password": password,
        "demand_encryption": demand_encryption,
        "encryption_type": encryption_type,
        "certificate": certificate,
        "client_certificate": client_certificate,
        "client_key": client_key,
        "network_type": network_type,
    }
    form = {"name": name}
    form.update(
        _collect(_REMOTE_CLUSTER_KEYS, _VALID_REMOTE_CLUSTER_KEYS, named, extra)
    )

    logger.info(f"Creating remote cluster {name!r}")
    settings = get_settings(ctx)
    result = create_remote_cluster_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        form=form,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"name": name, "body": form, "result": result}


def update_remote_cluster(
    ctx: Context,
    name: str,
    hostname: str | None = None,
    username: str | None = None,
    password: str | None = None,
    demand_encryption: bool | None = None,
    encryption_type: str | None = None,
    certificate: str | None = None,
    client_certificate: str | None = None,
    client_key: str | None = None,
    network_type: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update an existing remote-cluster reference.

    Only the supplied fields are sent; unspecified ones stay unchanged.
    At least one updatable field must be provided.
    """
    _require_write_scope()
    assert_remote_cluster_name(name)

    named = {
        "hostname": hostname,
        "username": username,
        "password": password,
        "demand_encryption": demand_encryption,
        "encryption_type": encryption_type,
        "certificate": certificate,
        "client_certificate": client_certificate,
        "client_key": client_key,
        "network_type": network_type,
    }
    form = _collect(_REMOTE_CLUSTER_KEYS, _VALID_REMOTE_CLUSTER_KEYS, named, extra)
    if not form:
        raise ValueError(
            "Provide at least one field to update (a named parameter or "
            "an 'extra' entry)."
        )

    logger.info(f"Updating remote cluster {name!r}")
    settings = get_settings(ctx)
    result = update_remote_cluster_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        name=name,
        form=form,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"name": name, "body": form, "result": result}


def delete_remote_cluster(
    ctx: Context,
    name: str,
    confirm: bool = False,
    confirm_name: str | None = None,
) -> dict[str, Any]:
    """Delete a remote-cluster reference. Requires ``confirm=True`` AND
    ``confirm_name`` matching the target reference name.

    Fails if any replication currently uses this remote cluster (Couchbase
    rejects deletion in that case).
    """
    _require_write_scope()
    assert_remote_cluster_name(name)

    if not confirm:
        raise ValueError(
            "delete_remote_cluster requires confirm=True. This removes the "
            "cross-cluster connection and any replications pointing at it."
        )
    if confirm_name != name:
        raise ValueError(
            "delete_remote_cluster requires confirm_name to exactly match "
            f"name ({name!r}). This guard against fat-finger deletion "
            "matches the delete_bucket pattern."
        )

    logger.warning(f"Deleting remote cluster {name!r}")
    settings = get_settings(ctx)
    result = delete_remote_cluster_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        name=name,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"name": name, "deleted": True, "result": result}


def list_remote_clusters(ctx: Context) -> dict[str, Any]:
    """List all remote-cluster references on this cluster.

    Returns a dict with the ``remote_clusters`` key holding the array of
    reference records returned by the cluster manager.
    """
    settings = get_settings(ctx)
    result = list_remote_clusters_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"remote_clusters": result}


# --------------------------------------------------------------------------
# Replication lifecycle tools
# --------------------------------------------------------------------------


def create_replication(
    ctx: Context,
    from_bucket: str,
    to_cluster: str,
    to_bucket: str,
    replication_type: str = "continuous",
    filter_expression: str | None = None,
    priority: str | None = None,
    compression_type: str | None = None,
    network_type: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a replication from ``from_bucket`` on this cluster to
    ``to_bucket`` on the named remote cluster (``to_cluster``).

    ``replication_type`` is ``"continuous"`` (default) or ``"one-time"``.

    Returns a dict with the body sent, the resulting replication ID from
    the server response, and the raw result.
    """
    _require_write_scope()

    if replication_type not in ("continuous", "one-time"):
        raise ValueError(
            f"replication_type must be 'continuous' or 'one-time', got "
            f"{replication_type!r}"
        )

    named = {
        "from_bucket": from_bucket,
        "to_cluster": to_cluster,
        "to_bucket": to_bucket,
        "replication_type": replication_type,
        "filter_expression": filter_expression,
        "priority": priority,
        "compression_type": compression_type,
        "network_type": network_type,
    }
    form = _collect(
        _CREATE_REPLICATION_KEYS, _VALID_CREATE_REPLICATION_KEYS, named, extra
    )

    logger.info(
        f"Creating XDCR: {from_bucket} -> {to_cluster}/{to_bucket} ({replication_type})"
    )
    settings = get_settings(ctx)
    result = create_replication_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        form=form,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    replication_id = result.get("id") if isinstance(result, dict) else None
    return {"body": form, "replication_id": replication_id, "result": result}


def delete_replication(
    ctx: Context,
    replication_id: str,
    confirm: bool = False,
    confirm_name: str | None = None,
) -> dict[str, Any]:
    """Cancel (delete) an active replication. Requires ``confirm=True`` AND
    ``confirm_name`` matching the replication ID exactly.
    """
    _require_write_scope()
    assert_replication_id(replication_id)

    if not confirm:
        raise ValueError(
            "delete_replication requires confirm=True. This cancels the "
            "replication and stops all data movement to the target."
        )
    if confirm_name != replication_id:
        raise ValueError(
            "delete_replication requires confirm_name to exactly match "
            f"replication_id ({replication_id!r}). This guard against "
            "fat-finger deletion matches the delete_bucket pattern."
        )

    logger.warning(f"Deleting replication {replication_id!r}")
    settings = get_settings(ctx)
    result = delete_replication_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        replication_id=replication_id,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"replication_id": replication_id, "deleted": True, "result": result}


def pause_replication(
    ctx: Context,
    replication_id: str,
) -> dict[str, Any]:
    """Pause a running replication (sets ``pauseRequested=true``).

    Reversible via ``resume_replication``. Data movement stops but the
    replication configuration and checkpoint state are preserved.
    """
    _require_write_scope()
    assert_replication_id(replication_id)

    logger.info(f"Pausing replication {replication_id!r}")
    settings = get_settings(ctx)
    result = update_replication_settings_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        replication_id=replication_id,
        form={"pauseRequested": True},
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"replication_id": replication_id, "paused": True, "result": result}


def resume_replication(
    ctx: Context,
    replication_id: str,
) -> dict[str, Any]:
    """Resume a paused replication (sets ``pauseRequested=false``)."""
    _require_write_scope()
    assert_replication_id(replication_id)

    logger.info(f"Resuming replication {replication_id!r}")
    settings = get_settings(ctx)
    result = update_replication_settings_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        replication_id=replication_id,
        form={"pauseRequested": False},
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"replication_id": replication_id, "resumed": True, "result": result}


def list_replications(ctx: Context) -> dict[str, Any]:
    """List all XDCR replications on this cluster.

    Reads ``/pools/default/tasks`` and filters to XDCR tasks. Returns a
    dict with the ``replications`` key.
    """
    settings = get_settings(ctx)
    result = list_replications_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"replications": result}


def get_replication_settings(
    ctx: Context,
    replication_id: str,
) -> dict[str, Any]:
    """Get the current settings for a replication.

    Returns the raw settings dict from ``GET /settings/replications/{id}``.
    Includes throttling, filter expressions, priority, and pause state.
    """
    assert_replication_id(replication_id)
    settings = get_settings(ctx)
    return get_replication_settings_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        replication_id=replication_id,
        ca_cert_path=settings.get("ca_cert_path"),
    )


def update_replication_settings(
    ctx: Context,
    replication_id: str,
    pause_requested: bool | None = None,
    priority: str | None = None,
    compression_type: str | None = None,
    filter_expression: str | None = None,
    filter_skip_restream: bool | None = None,
    checkpoint_interval: int | None = None,
    worker_batch_size: int | None = None,
    doc_batch_size_kb: int | None = None,
    failure_restart_interval: int | None = None,
    source_nozzle_per_node: int | None = None,
    target_nozzle_per_node: int | None = None,
    log_level: str | None = None,
    stats_interval: int | None = None,
    network_type: str | None = None,
    collections_mapping_rules: dict[str, Any] | None = None,
    collections_migration_mode: bool | None = None,
    collections_mirroring_mode: bool | None = None,
    collections_explicit_mapping: bool | None = None,
    collections_oso_mode: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update a subset of a replication's settings.

    Only the fields supplied are sent to the server. At least one setting
    must be provided. Prefer the named parameters; ``extra`` exists for
    forward compatibility with settings not yet exposed here.

    Returns a dict with the body sent and the server response.
    """
    _require_write_scope()
    assert_replication_id(replication_id)

    named = {
        "pause_requested": pause_requested,
        "priority": priority,
        "compression_type": compression_type,
        "filter_expression": filter_expression,
        "filter_skip_restream": filter_skip_restream,
        "checkpoint_interval": checkpoint_interval,
        "worker_batch_size": worker_batch_size,
        "doc_batch_size_kb": doc_batch_size_kb,
        "failure_restart_interval": failure_restart_interval,
        "source_nozzle_per_node": source_nozzle_per_node,
        "target_nozzle_per_node": target_nozzle_per_node,
        "log_level": log_level,
        "stats_interval": stats_interval,
        "network_type": network_type,
        "collections_mapping_rules": collections_mapping_rules,
        "collections_migration_mode": collections_migration_mode,
        "collections_mirroring_mode": collections_mirroring_mode,
        "collections_explicit_mapping": collections_explicit_mapping,
        "collections_oso_mode": collections_oso_mode,
    }
    form = _collect(
        _REPLICATION_SETTINGS_KEYS,
        _VALID_REPLICATION_SETTINGS_KEYS,
        named,
        extra,
    )
    if not form:
        raise ValueError(
            "Provide at least one setting to update (a named parameter or "
            "an 'extra' entry)."
        )

    logger.info(f"Updating replication settings for {replication_id!r}")
    settings = get_settings(ctx)
    result = update_replication_settings_rest(
        connection_string=settings["connection_string"],
        username=settings["username"],
        password=settings["password"],
        replication_id=replication_id,
        form=form,
        ca_cert_path=settings.get("ca_cert_path"),
    )
    return {"replication_id": replication_id, "body": form, "result": result}
