"""
REST client for the Cluster Manager bucket-lifecycle endpoints.

Bucket lifecycle is served by the cluster manager on port 8091 (18091 for
TLS) at ``/pools/default/buckets`` (create/list/update/delete),
``/pools/default/buckets/{name}/controller/doFlush`` (flush),
``/pools/default/buckets/{name}/controller/compactBucket`` and
``.../cancelBucketCompaction`` (compact),  and ``/sampleBuckets/install``
(sample-data loader).

Verified against Couchbase Server docs (rest-api/rest-bucket-create,
rest-bucket-update, rest-bucket-delete, rest-bucket-flush,
rest-bucket-compaction, rest-sample-buckets) 2026-07-06.

This helper reuses the connection-string host extraction and SSL
verification logic already present in ``index_utils`` so behavior (Capella
root CA handling, multi-host fallback, TLS detection) stays consistent with
the existing index REST path.

License: MIT - Copyright (c) 2026 Chris Ahrendt
"""

import json
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from .constants import MCP_SERVER_NAME
from .index_utils import (
    _determine_ssl_verification,
    _extract_hosts_from_connection_string,
)

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.bucket_admin_rest")

# Cluster-manager ports for bucket lifecycle (same as GSI settings).
_MGMT_PORT = 8091
_MGMT_TLS_PORT = 18091

_BUCKETS_PATH = "/pools/default/buckets"
_SAMPLE_BUCKETS_PATH = "/sampleBuckets/install"

# Bucket name character class per Couchbase docs: letters, digits, and
# ``.``, ``-``, ``_``, ``%``. Length 1-100 chars. Reject anything else
# BEFORE it enters a URL path to avoid any injection surface.
_BUCKET_NAME_RE = re.compile(r"^[A-Za-z0-9._\-%]{1,100}$")

# Documented sample buckets in Couchbase Server 7.x/8.x. New samples added
# by future server versions should be added here explicitly.
ALLOWED_SAMPLE_BUCKETS: frozenset[str] = frozenset(
    {"travel-sample", "beer-sample", "gamesim-sample"}
)

# Compaction verbs. Kept as a small allow-list rather than accepting any
# string so a caller can't smuggle an arbitrary controller path.
ALLOWED_COMPACT_ACTIONS: frozenset[str] = frozenset({"start", "cancel"})


# --------------------------------------------------------------------------
# Validators (imported by bucket_admin.py; keeping them near the REST layer
# so the allow-lists and the encoding stay coupled)
# --------------------------------------------------------------------------


def assert_bucket_name(name: str) -> None:
    """Reject bucket names that don't match Couchbase's documented charset.

    Also prevents path traversal / injection via bucket_name since the value
    is interpolated into a REST URL path.
    """
    if not isinstance(name, str) or not _BUCKET_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid bucket name {name!r}. Bucket names must be 1-100 chars "
            "of [A-Za-z0-9._%-]."
        )


def _encode_path_segment(value: str) -> str:
    """URL-encode a value for use as a REST path segment.

    Couchbase documents ``%`` as a valid bucket-name character. When a caller
    provides a name like ``%2f%2e%2e`` (URL-encoded ``/../``), the raw
    interpolation would send that literal to the server; some HTTP frameworks
    decode-then-route, opening a path-confusion vector even though the caller
    is already authenticated. Encoding the segment with ``safe=""`` produces
    ``%252f%252e%252e``, which the server treats as an opaque bucket name.
    """
    return quote(value, safe="")


def validate_extra_bucket_keys(extra: dict[str, Any], allowed: frozenset[str]) -> None:
    """Reject any key in ``extra`` that is not in ``allowed``.

    The named parameters cover the documented parameter set; ``extra``
    exists for forward-compatibility. Any unknown key is rejected to
    prevent posting arbitrary form fields to the bucket endpoint.
    """
    unknown = sorted(set(extra) - allowed)
    if unknown:
        raise ValueError(
            f"Unknown bucket setting key(s) in 'extra' or 'body': {unknown}. "
            f"Allowed keys are {sorted(allowed)}. Use a named parameter "
            "where one exists."
        )


# --------------------------------------------------------------------------
# Small internals shared by every REST call
# --------------------------------------------------------------------------


def _mgmt_base(connection_string: str) -> tuple[str, int]:
    """Return (scheme, port) for the cluster-manager endpoint."""
    is_tls = connection_string.lower().startswith("couchbases://")
    return ("https", _MGMT_TLS_PORT) if is_tls else ("http", _MGMT_PORT)


def _form_value(value: Any) -> str:
    """Render a value for the form-urlencoded body.

    Bucket endpoints are inconsistent — flushEnabled takes 0/1, most others
    take true/false, ram-quota takes an int. We render:
    - bool  -> "true"/"false" (endpoint accepts either 0/1 or true/false for flushEnabled)
    - int   -> str(int)
    - dict  -> json.dumps (for autoCompactionSettings, etc.)
    - other -> str()
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value)


def _request(
    *,
    method: str,
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None,
    timeout: int,
    path: str,
    data: dict[str, str] | None = None,
    expected_ok: tuple[int, ...] = (200, 202),
) -> dict[str, Any]:
    """Issue a REST request to the cluster manager, trying each host in turn.

    On success returns the parsed JSON body when present, or an empty dict
    (many bucket endpoints return an empty body on 202 Accepted). Raises
    ``RuntimeError`` if every host fails.
    """
    hosts = _extract_hosts_from_connection_string(connection_string)
    scheme, port = _mgmt_base(connection_string)
    verify_ssl = _determine_ssl_verification(connection_string, ca_cert_path)

    last_error: Exception | None = None
    with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
        for host in hosts:
            url = f"{scheme}://{host}:{port}{path}"
            try:
                logger.info(f"{method} {url}")
                if method == "GET":
                    resp = client.get(url, auth=(username, password))
                elif method == "DELETE":
                    resp = client.delete(url, auth=(username, password))
                elif method == "POST":
                    resp = client.post(url, data=data, auth=(username, password))
                else:
                    raise ValueError(f"unsupported HTTP method: {method}")
                if resp.status_code not in expected_ok:
                    resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if resp.content and content_type.startswith("application/json"):
                    return resp.json()
                return {}
            except httpx.HTTPError as e:
                logger.warning(f"Bucket-admin {method} failed on {host}: {e}")
                last_error = e

    error_msg = f"Bucket-admin {method} {path} failed on all hosts: {hosts}"
    if last_error:
        error_msg += f". Last error: {last_error}"
    logger.error(error_msg)
    raise RuntimeError(error_msg)


# --------------------------------------------------------------------------
# Public functions used by tools/bucket_admin.py
# --------------------------------------------------------------------------


def create_bucket_rest(
    connection_string: str,
    username: str,
    password: str,
    form: dict[str, Any],
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST /pools/default/buckets to create a bucket."""
    if "name" not in form:
        raise ValueError("form must include 'name'")
    body = {k: _form_value(v) for k, v in form.items() if v is not None}
    return _request(
        method="POST",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=_BUCKETS_PATH,
        data=body,
    )


def update_bucket_rest(
    connection_string: str,
    username: str,
    password: str,
    bucket_name: str,
    form: dict[str, Any],
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST /pools/default/buckets/{name} to update an existing bucket."""
    assert_bucket_name(bucket_name)
    if not form:
        raise ValueError("form must contain at least one setting to update")
    body = {k: _form_value(v) for k, v in form.items() if v is not None}
    return _request(
        method="POST",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=f"{_BUCKETS_PATH}/{_encode_path_segment(bucket_name)}",
        data=body,
    )


def delete_bucket_rest(
    connection_string: str,
    username: str,
    password: str,
    bucket_name: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """DELETE /pools/default/buckets/{name}."""
    assert_bucket_name(bucket_name)
    return _request(
        method="DELETE",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=f"{_BUCKETS_PATH}/{_encode_path_segment(bucket_name)}",
    )


def flush_bucket_rest(
    connection_string: str,
    username: str,
    password: str,
    bucket_name: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST /pools/default/buckets/{name}/controller/doFlush."""
    assert_bucket_name(bucket_name)
    return _request(
        method="POST",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=f"{_BUCKETS_PATH}/{_encode_path_segment(bucket_name)}/controller/doFlush",
    )


def compact_bucket_rest(
    connection_string: str,
    username: str,
    password: str,
    bucket_name: str,
    action: str,
    ca_cert_path: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST /pools/default/buckets/{name}/controller/{compactBucket|cancelBucketCompaction}."""
    assert_bucket_name(bucket_name)
    if action not in ALLOWED_COMPACT_ACTIONS:
        raise ValueError(
            f"action must be one of {sorted(ALLOWED_COMPACT_ACTIONS)}, got {action!r}"
        )
    controller = "compactBucket" if action == "start" else "cancelBucketCompaction"
    return _request(
        method="POST",
        connection_string=connection_string,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
        timeout=timeout,
        path=f"{_BUCKETS_PATH}/{_encode_path_segment(bucket_name)}/controller/{controller}",
    )


def load_sample_bucket_rest(
    connection_string: str,
    username: str,
    password: str,
    sample_name: str,
    ca_cert_path: str | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """POST /sampleBuckets/install with the sample name.

    Body is a JSON array of sample names, per Couchbase docs. Response is a
    202 Accepted with an empty or task-list body; the sample loads
    asynchronously.
    """
    if sample_name not in ALLOWED_SAMPLE_BUCKETS:
        raise ValueError(
            f"sample_name must be one of {sorted(ALLOWED_SAMPLE_BUCKETS)}, "
            f"got {sample_name!r}"
        )
    hosts = _extract_hosts_from_connection_string(connection_string)
    scheme, port = _mgmt_base(connection_string)
    verify_ssl = _determine_ssl_verification(connection_string, ca_cert_path)
    payload = json.dumps([sample_name])

    last_error: Exception | None = None
    with httpx.Client(verify=verify_ssl, timeout=timeout) as client:
        for host in hosts:
            url = f"{scheme}://{host}:{port}{_SAMPLE_BUCKETS_PATH}"
            try:
                logger.info(f"POST {url} (sample={sample_name})")
                resp = client.post(
                    url,
                    content=payload,
                    headers={"content-type": "application/json"},
                    auth=(username, password),
                )
                if resp.status_code not in (200, 202):
                    resp.raise_for_status()
                if resp.content and resp.headers.get("content-type", "").startswith(
                    "application/json"
                ):
                    return resp.json()
                return {}
            except httpx.HTTPError as e:
                logger.warning(f"Sample-bucket install failed on {host}: {e}")
                last_error = e

    error_msg = f"Sample-bucket install failed on all hosts: {hosts}"
    if last_error:
        error_msg += f". Last error: {last_error}"
    logger.error(error_msg)
    raise RuntimeError(error_msg)
