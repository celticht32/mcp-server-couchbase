"""
Couchbase MCP Tools

This module contains all the MCP tools for Couchbase operations.

Tool Categories:
- READ_ONLY_TOOLS: Tools that only read data (always available)
- KV_WRITE_TOOLS: KV tools that modify data (disabled when READ_ONLY_MODE=True)
"""

from collections.abc import Callable

from mcp.types import ToolAnnotations

# Scope and collection management tools (write) + get_collection_settings (read)
from .collections_admin import (
    create_collection,
    create_scope,
    drop_collection,
    drop_scope,
    get_collection_settings,
    update_collection,
)

# Index tools
from .index import get_index_advisor_recommendations, list_indexes

# Index management tools (DDL + GSI settings, write)
from .index_admin import (
    admin_index_settings_get,
    admin_index_settings_set,
    build_deferred_indexes,
    create_index,
    drop_index,
)

# Key-Value tools
from .kv import (
    delete_document_by_id,
    get_document_by_id,
    insert_document_by_id,
    replace_document_by_id,
    upsert_document_by_id,
)

# Query tools
from .query import (
    explain_sql_plus_plus_query,
    get_longest_running_queries,
    get_most_frequent_queries,
    get_queries_not_selective,
    get_queries_not_using_covering_index,
    get_queries_using_primary_index,
    get_queries_with_large_result_count,
    get_queries_with_largest_response_sizes,
    get_schema_for_collection,
    run_sql_plus_plus_query,
)

# Server tools
from .server import (
    get_buckets_in_cluster,
    get_cluster_health_and_services,
    get_collections_in_scope,
    get_scopes_and_collections_in_bucket,
    get_scopes_in_bucket,
    get_server_configuration_status,
    test_cluster_connection,
)

# XDCR admin tools (remote clusters + replications, write) + list/get reads
from .xdcr_admin import (
    create_remote_cluster,
    create_replication,
    delete_remote_cluster,
    delete_replication,
    get_replication_settings,
    list_remote_clusters,
    list_replications,
    pause_replication,
    resume_replication,
    update_remote_cluster,
    update_replication_settings,
)

# Read-only tools - always available regardless of mode settings
READ_ONLY_TOOLS = [
    # Server/Cluster management tools
    get_buckets_in_cluster,
    get_server_configuration_status,
    test_cluster_connection,
    get_scopes_and_collections_in_bucket,
    get_collections_in_scope,
    get_scopes_in_bucket,
    get_cluster_health_and_services,
    # KV read tool
    get_document_by_id,
    # Query tools (read operations)
    get_schema_for_collection,
    run_sql_plus_plus_query,  # Write protection handled at runtime via read_only_mode
    explain_sql_plus_plus_query,
    # Index tools
    get_index_advisor_recommendations,
    list_indexes,
    # Index settings (read)
    admin_index_settings_get,
    # Collection settings (read)
    get_collection_settings,
    # XDCR read tools
    list_remote_clusters,
    list_replications,
    get_replication_settings,
    # Query performance analysis tools
    get_queries_not_selective,
    get_queries_not_using_covering_index,
    get_queries_using_primary_index,
    get_queries_with_large_result_count,
    get_queries_with_largest_response_sizes,
    get_longest_running_queries,
    get_most_frequent_queries,
]

# KV write tools - disabled when READ_ONLY_MODE is True
KV_WRITE_TOOLS = [
    upsert_document_by_id,
    insert_document_by_id,
    replace_document_by_id,
    delete_document_by_id,
]

# Admin write tools - loaded only when read_only_mode is False AND
# admin_write_mode is True. These mutate cluster structure (index DDL) or
# cluster-wide index settings, a strictly higher privilege than data writes,
# so they gate behind a second, independent flag.
ADMIN_WRITE_TOOLS = [
    create_index,
    drop_index,
    build_deferred_indexes,
    admin_index_settings_set,
    create_scope,
    drop_scope,
    create_collection,
    drop_collection,
    update_collection,
    create_remote_cluster,
    update_remote_cluster,
    delete_remote_cluster,
    create_replication,
    delete_replication,
    pause_replication,
    resume_replication,
    update_replication_settings,
]

# List of all tools for easy registration (kept for backward compatibility)
ALL_TOOLS = READ_ONLY_TOOLS + KV_WRITE_TOOLS

# Tool annotations for MCP clients (readOnlyHint, destructiveHint, etc.)
TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    # Server/Cluster management tools (read-only)
    "get_server_configuration_status": ToolAnnotations(readOnlyHint=True),
    "test_cluster_connection": ToolAnnotations(readOnlyHint=True),
    "get_buckets_in_cluster": ToolAnnotations(readOnlyHint=True),
    "get_scopes_and_collections_in_bucket": ToolAnnotations(readOnlyHint=True),
    "get_collections_in_scope": ToolAnnotations(readOnlyHint=True),
    "get_scopes_in_bucket": ToolAnnotations(readOnlyHint=True),
    "get_cluster_health_and_services": ToolAnnotations(readOnlyHint=True),
    # KV read tool
    "get_document_by_id": ToolAnnotations(readOnlyHint=True),
    # Query tools
    "get_schema_for_collection": ToolAnnotations(readOnlyHint=True),
    "run_sql_plus_plus_query": ToolAnnotations(),
    "explain_sql_plus_plus_query": ToolAnnotations(readOnlyHint=True),
    # Index tools (read-only)
    "get_index_advisor_recommendations": ToolAnnotations(readOnlyHint=True),
    "list_indexes": ToolAnnotations(readOnlyHint=True),
    # Query performance analysis tools (read-only)
    "get_longest_running_queries": ToolAnnotations(readOnlyHint=True),
    "get_most_frequent_queries": ToolAnnotations(readOnlyHint=True),
    "get_queries_with_largest_response_sizes": ToolAnnotations(readOnlyHint=True),
    "get_queries_with_large_result_count": ToolAnnotations(readOnlyHint=True),
    "get_queries_using_primary_index": ToolAnnotations(readOnlyHint=True),
    "get_queries_not_using_covering_index": ToolAnnotations(readOnlyHint=True),
    "get_queries_not_selective": ToolAnnotations(readOnlyHint=True),
    # KV write tools
    "upsert_document_by_id": ToolAnnotations(idempotentHint=True),
    "insert_document_by_id": ToolAnnotations(idempotentHint=True),
    "replace_document_by_id": ToolAnnotations(idempotentHint=True),
    "delete_document_by_id": ToolAnnotations(destructiveHint=True, idempotentHint=True),
    # Index management tools (write)
    "create_index": ToolAnnotations(idempotentHint=False),
    "drop_index": ToolAnnotations(destructiveHint=True, idempotentHint=True),
    "build_deferred_indexes": ToolAnnotations(idempotentHint=True),
    # Index settings
    "admin_index_settings_get": ToolAnnotations(readOnlyHint=True),
    "admin_index_settings_set": ToolAnnotations(
        destructiveHint=False, idempotentHint=True
    ),
    # Scope/collection management (write)
    "create_scope": ToolAnnotations(idempotentHint=False),
    "drop_scope": ToolAnnotations(destructiveHint=True, idempotentHint=True),
    "create_collection": ToolAnnotations(idempotentHint=False),
    "drop_collection": ToolAnnotations(destructiveHint=True, idempotentHint=True),
    "update_collection": ToolAnnotations(destructiveHint=False, idempotentHint=True),
    # Collection settings (read)
    "get_collection_settings": ToolAnnotations(readOnlyHint=True),
    # XDCR read tools
    "list_remote_clusters": ToolAnnotations(readOnlyHint=True),
    "list_replications": ToolAnnotations(readOnlyHint=True),
    "get_replication_settings": ToolAnnotations(readOnlyHint=True),
    # XDCR remote-cluster tools (write)
    "create_remote_cluster": ToolAnnotations(idempotentHint=False),
    "update_remote_cluster": ToolAnnotations(
        destructiveHint=False, idempotentHint=True
    ),
    "delete_remote_cluster": ToolAnnotations(destructiveHint=True, idempotentHint=True),
    # XDCR replication lifecycle (write)
    "create_replication": ToolAnnotations(idempotentHint=False),
    "delete_replication": ToolAnnotations(destructiveHint=True, idempotentHint=True),
    "pause_replication": ToolAnnotations(destructiveHint=False, idempotentHint=True),
    "resume_replication": ToolAnnotations(destructiveHint=False, idempotentHint=True),
    "update_replication_settings": ToolAnnotations(
        destructiveHint=False, idempotentHint=True
    ),
}


def get_tools(
    read_only_mode: bool = True,
    admin_write_mode: bool = False,
) -> list[Callable]:
    """Get the list of tools based on the mode settings.

    - READ_ONLY_TOOLS are always loaded.
    - KV_WRITE_TOOLS load when read_only_mode is False.
    - ADMIN_WRITE_TOOLS load only when read_only_mode is False AND
      admin_write_mode is True. Cluster-structure mutation (index DDL) and
      cluster-wide index settings are a higher privilege than data writes, so
      disabling read-only alone does not expose them; an operator must also
      opt in to admin writes.
    """
    tools = list(READ_ONLY_TOOLS)

    if not read_only_mode:
        # KV write tools are only loaded when READ_ONLY_MODE is False
        tools.extend(KV_WRITE_TOOLS)
        if admin_write_mode:
            tools.extend(ADMIN_WRITE_TOOLS)

    return tools


__all__ = [
    # Individual tools
    "get_server_configuration_status",
    "test_cluster_connection",
    "get_scopes_and_collections_in_bucket",
    "get_collections_in_scope",
    "get_scopes_in_bucket",
    "get_buckets_in_cluster",
    "get_document_by_id",
    "upsert_document_by_id",
    "insert_document_by_id",
    "replace_document_by_id",
    "delete_document_by_id",
    "get_schema_for_collection",
    "run_sql_plus_plus_query",
    "explain_sql_plus_plus_query",
    "get_index_advisor_recommendations",
    "list_indexes",
    "create_index",
    "drop_index",
    "build_deferred_indexes",
    "admin_index_settings_get",
    "admin_index_settings_set",
    "create_scope",
    "drop_scope",
    "create_collection",
    "drop_collection",
    "update_collection",
    "get_collection_settings",
    "list_remote_clusters",
    "list_replications",
    "get_replication_settings",
    "create_remote_cluster",
    "update_remote_cluster",
    "delete_remote_cluster",
    "create_replication",
    "delete_replication",
    "pause_replication",
    "resume_replication",
    "update_replication_settings",
    "get_cluster_health_and_services",
    "get_queries_not_selective",
    "get_queries_not_using_covering_index",
    "get_queries_using_primary_index",
    "get_queries_with_large_result_count",
    "get_queries_with_largest_response_sizes",
    "get_longest_running_queries",
    "get_most_frequent_queries",
    # Tool categories
    "READ_ONLY_TOOLS",
    "KV_WRITE_TOOLS",
    "ADMIN_WRITE_TOOLS",
    # Tool annotations
    "TOOL_ANNOTATIONS",
    # Convenience
    "ALL_TOOLS",
    "get_tools",
]
