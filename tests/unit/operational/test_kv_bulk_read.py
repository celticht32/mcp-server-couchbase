"""Unit tests for the optional batch parameter on get_document_by_id.

CBSE-23739, PR #274, reshaped. Adds no tools.

The tests are built around the four things that make this a different
capability from `SELECT * FROM b USE KEYS [...]` rather than a duplicate of it:
a bound on the result, per-key outcomes, no dependence on the Query service,
and no dependence on a query privilege the KV principal may not hold.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from couchbase.exceptions import DocumentNotFoundException

from cb_mcp.tools.operational.kv import MAX_BULK_READ_DOCS, get_document_by_id


def _ctx() -> tuple[SimpleNamespace, MagicMock, MagicMock]:
    cluster = MagicMock()
    bucket = MagicMock()
    collection = MagicMock()
    bucket.scope.return_value.collection.return_value = collection
    cluster.bucket.return_value = bucket
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                cluster_provider=SimpleNamespace(get_cluster=lambda c: cluster),
            )
        )
    )
    return ctx, cluster, collection


def _conn(cluster: MagicMock):
    return patch(
        "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
    )


def _doc(content: dict) -> SimpleNamespace:
    return SimpleNamespace(content_as={dict: content})


def _multi(found: dict[str, dict], failed: dict[str, Exception]) -> SimpleNamespace:
    return SimpleNamespace(
        results={k: _doc(v) for k, v in found.items()},
        exceptions=dict(failed),
    )


class TestSingleDocumentContractIsUnchanged:
    def test_positional_call_returns_the_bare_document(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get.return_value = _doc({"field": "value"})
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", "doc1")
        assert result == {"field": "value"}

    def test_not_found_still_raises(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get.side_effect = DocumentNotFoundException()
        with _conn(cluster), pytest.raises(DocumentNotFoundException):
            get_document_by_id(ctx, "b", "s", "c", "doc1")

    def test_the_batch_sdk_call_is_not_reached(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get.return_value = _doc({})
        with _conn(cluster):
            get_document_by_id(ctx, "b", "s", "c", "doc1")
        collection.get_multi.assert_not_called()


class TestBatchIsOneCall:
    def test_three_documents_go_in_one_sdk_call(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi(
            {"a": {"n": 1}, "b": {"n": 2}, "c": {"n": 3}}, {}
        )
        with _conn(cluster):
            result = get_document_by_id(
                ctx, "b", "s", "c", document_ids=["a", "b", "c"]
            )
        assert result["documents"] == {"a": {"n": 1}, "b": {"n": 2}, "c": {"n": 3}}
        assert result["errors"] == {}
        assert collection.get_multi.call_count == 1
        collection.get.assert_not_called()

    def test_exceptions_are_requested_from_the_sdk(self) -> None:
        """Without return_exceptions the first missing document raises and the
        rest of the batch's outcomes are lost."""
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi({"a": {}}, {})
        with _conn(cluster):
            get_document_by_id(ctx, "b", "s", "c", document_ids=["a"])
        _, kwargs = collection.get_multi.call_args
        assert kwargs["return_exceptions"] is True

    def test_a_repeated_id_is_read_once(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi({"a": {}, "b": {}}, {})
        with _conn(cluster):
            get_document_by_id(ctx, "b", "s", "c", document_ids=["a", "b", "a"])
        args, _ = collection.get_multi.call_args
        assert args[0] == ["a", "b"]


class TestPerKeyOutcomes:
    """The capability SQL++ USE KEYS cannot offer: a key that is absent simply
    produces no row there, so the caller is left diffing and still cannot tell
    'absent' from 'I mistyped it'."""

    def test_every_requested_id_lands_in_exactly_one_bucket(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi(
            {"a": {"n": 1}, "c": {"n": 3}}, {"b": DocumentNotFoundException()}
        )
        with _conn(cluster):
            result = get_document_by_id(
                ctx, "b", "s", "c", document_ids=["a", "b", "c"]
            )
        asked = {"a", "b", "c"}
        assert set(result["documents"]) | set(result["errors"]) == asked
        assert not set(result["documents"]) & set(result["errors"])

    def test_a_missing_document_is_named_and_explained(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi(
            {}, {"gone": DocumentNotFoundException()}
        )
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=["gone"])
        assert "DocumentNotFoundException" in result["errors"]["gone"]

    def test_a_batch_never_raises_for_one_document(self) -> None:
        """The single form raises on not-found, which is right for one document
        and wrong for fifty: one absent key must not discard 49 answers."""
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi(
            {"a": {"n": 1}}, {"b": DocumentNotFoundException()}
        )
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=["a", "b"])
        assert result["documents"] == {"a": {"n": 1}}
        assert "b" in result["errors"]

    def test_an_id_with_no_reported_outcome_is_an_error_not_an_absence(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi({"a": {}}, {})
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=["a", "ghost"])
        assert "ghost" in result["errors"]
        assert "does not establish that it is absent" in result["errors"]["ghost"]

    def test_an_undecodable_document_is_an_error_not_a_crash(self) -> None:
        ctx, cluster, collection = _ctx()
        bad = SimpleNamespace()
        collection.get_multi.return_value = SimpleNamespace(
            results={"a": bad}, exceptions={}
        )
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=["a"])
        assert result["success"] is True
        assert "a" in result["errors"]


class TestTheResultIsBounded:
    """If result size and context pressure are the concern, the bounded option
    is this tool. SELECT * ... USE KEYS has no equivalent cap."""

    def test_over_the_limit_is_refused_before_the_cluster_is_touched(self) -> None:
        ctx, cluster, collection = _ctx()
        ids = [f"doc{i}" for i in range(MAX_BULK_READ_DOCS + 1)]
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=ids)
        assert result["success"] is False
        assert str(MAX_BULK_READ_DOCS) in result["error"]
        collection.get_multi.assert_not_called()

    def test_exactly_the_limit_is_allowed(self) -> None:
        ctx, cluster, collection = _ctx()
        ids = [f"doc{i}" for i in range(MAX_BULK_READ_DOCS)]
        collection.get_multi.return_value = _multi({i: {} for i in ids}, {})
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=ids)
        assert result["success"] is True

    def test_an_empty_batch_is_refused(self) -> None:
        ctx, cluster, collection = _ctx()
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=[])
        assert result["success"] is False
        collection.get_multi.assert_not_called()

    def test_both_modes_at_once_is_refused(self) -> None:
        ctx, cluster, collection = _ctx()
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", "doc1", document_ids=["a"])
        assert result["success"] is False
        assert "not both" in result["error"]
        collection.get.assert_not_called()
        collection.get_multi.assert_not_called()

    def test_neither_mode_is_refused(self) -> None:
        ctx, cluster, collection = _ctx()
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c")
        assert result["success"] is False
        collection.get.assert_not_called()
        collection.get_multi.assert_not_called()


class TestDocstringSteersTheModel:
    @staticmethod
    def _doc() -> str:
        return " ".join((get_document_by_id.__doc__ or "").split())

    def test_the_batch_form_is_advertised(self) -> None:
        assert "Prefer the batch form" in self._doc()

    def test_the_documented_limit_matches_the_enforced_one(self) -> None:
        assert f"up to {MAX_BULK_READ_DOCS} documents" in self._doc()

    def test_the_two_return_shapes_are_distinguished(self) -> None:
        doc = self._doc()
        assert "Single form returns the document" in doc
        assert '"documents": {id: content}' in doc

    def test_no_template_placeholder_survived(self) -> None:
        raw = get_document_by_id.__doc__ or ""
        assert "<<" not in raw
