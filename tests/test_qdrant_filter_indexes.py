# tests/test_qdrant_filter_indexes.py
"""
Tests for qdrant filter-field payload indexes (C9 follow-up -- project-mine
skip-check performance).

`file_already_mined`, its in-lock recheck, and stale-drawer purges all filter
on ``metadata.source_file``. On an unindexed payload field qdrant full-scans
the collection for every such query (~5-9s per file at CVP scale, 458k
points). QdrantCollection now ensures a keyword payload index per
_FILTER_INDEX_FIELDS with these guarantees, each covered here:

  1. Once-per-collection creation, decided by SERVER state: an open checks
     the collection's index list and creates only what is missing. In
     steady state (index present) zero creation calls are ever issued --
     any process, any host.
  2. New collections create the filter indexes at birth (no listing needed).
  3. Any failure (listing or creation) degrades to today's unindexed-scan
     behavior with a warning -- never a failed mine.
"""

import types
import sys
from unittest import mock

import pytest


# ── Stub heavy deps so we can import mempalace modules in isolation ─────────
# Same pattern as tests/test_qdrant_bulk_metadata_scroll.py (see that file's
# documented rationale: autouse fixture, monkeypatch.setitem teardown).
_STUB_MODULE_NAMES = [
    "mempalace.knowledge_graph",
    "mempalace.searcher",
    "mempalace.palace_graph",
    "mempalace.config",
]


def _build_stub(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    m.KnowledgeGraph = lambda: types.SimpleNamespace()  # type: ignore[attr-defined]
    m.search_memories = lambda *a, **kw: []  # type: ignore[attr-defined]
    m.traverse = lambda *a, **kw: {}  # type: ignore[attr-defined]
    m.find_tunnels = lambda *a, **kw: {}  # type: ignore[attr-defined]
    m.graph_stats = lambda *a, **kw: {}  # type: ignore[attr-defined]
    m.MempalaceConfig = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
        palace_path="~/.mempalace/palace", collection_name="mempalace"
    )
    return m


@pytest.fixture(autouse=True)
def _stub_heavy_deps(monkeypatch):
    for name in _STUB_MODULE_NAMES:
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, _build_stub(name))
    yield


import numpy  # noqa: E402,F401

from mempalace.backends import qdrant as qdrant_mod  # noqa: E402
from mempalace.backends.base import PalaceRef  # noqa: E402
from mempalace.backends.qdrant import QdrantCollection, _QdrantConfig  # noqa: E402


SOURCE_INDEX_FIELD = f"{qdrant_mod._PAYLOAD_METADATA}.source_file"


def _make_qdrant_collection(scroll_pages):
    """QdrantCollection with a mocked REST client (same shape as
    test_qdrant_bulk_metadata_scroll._make_qdrant_collection). scroll_pages:
    list[tuple[list[dict_point], next_offset]] served in order."""
    config = _QdrantConfig(url="http://localhost:6333")
    client = mock.MagicMock()
    client.scroll_points.side_effect = lambda collection, **kwargs: (
        scroll_pages.pop(0) if scroll_pages else ([], None)
    )
    client.collection_exists.return_value = True
    client.list_payload_indexes.return_value = [SOURCE_INDEX_FIELD]

    backend = mock.MagicMock()
    backend._closed = False
    backend._marker_exists.return_value = True

    palace = PalaceRef(id="/tmp/fake-palace", local_path="/tmp/fake-palace")
    col = QdrantCollection(
        backend=backend,
        client=client,
        config=config,
        palace=palace,
        collection_name="mempalace",
        remote_collection="mempalace_abc123_mempalace",
    )
    return col, client


def _fake_point(doc_id: str) -> dict:
    return {
        "id": f"point-{doc_id}",
        "payload": {
            qdrant_mod._PAYLOAD_ID: doc_id,
            qdrant_mod._PAYLOAD_DOCUMENT: f"content for {doc_id}",
            qdrant_mod._PAYLOAD_METADATA: {"source_file": f"/t/{doc_id}.jsonl"},
        },
        "vector": None,
    }


def _index_creates(client) -> list[tuple]:
    return [call.args for call in client.create_payload_index.call_args_list]


class TestFilterIndexOncePerCollection:
    def test_missing_index_created_exactly_once(self):
        """Index absent server-side -> created on first open, and a second
        operation on the same object issues NO further creation calls."""
        page = ([_fake_point("d0")], None)
        col, client = _make_qdrant_collection([page])
        client.list_payload_indexes.return_value = []

        col.get_all_metadata()
        col.get_all_metadata()

        creates = [c for c in _index_creates(client) if c[1] == SOURCE_INDEX_FIELD]
        assert creates == [(col._remote_collection, SOURCE_INDEX_FIELD, "keyword")]

    def test_present_index_issues_zero_creation_calls(self):
        """Steady state (every later open, any host): the listing already
        shows the index, so ZERO create calls are ever issued."""
        page = ([_fake_point("d0")], None)
        col, client = _make_qdrant_collection([page])
        client.list_payload_indexes.return_value = [SOURCE_INDEX_FIELD, "document"]

        col.get_all_metadata()
        col.get_all_metadata()

        client.create_payload_index.assert_not_called()


class TestFilterIndexFailureDegradation:
    def test_listing_failure_degrades_to_unindexed_with_warning(self, caplog):
        page = ([_fake_point("d0")], None)
        col, client = _make_qdrant_collection([page])
        client.list_payload_indexes.side_effect = RuntimeError("server hiccup")

        result = col.get_all_metadata()

        assert [m["source_file"] for m in result] == ["/t/d0.jsonl"]
        client.create_payload_index.assert_not_called()
        assert "could not list payload indexes" in caplog.text

    def test_creation_failure_degrades_to_unindexed_with_warning(self, caplog):
        page = ([_fake_point("d0")], None)
        col, client = _make_qdrant_collection([page])
        client.list_payload_indexes.return_value = []
        client.create_payload_index.side_effect = RuntimeError("read-only user")

        result = col.get_all_metadata()

        assert [m["source_file"] for m in result] == ["/t/d0.jsonl"]
        assert "could not ensure payload index" in caplog.text

    def test_absent_remote_collection_skips_index_calls(self):
        """No remote collection (and no marker -> genuinely empty state, not
        the corrupted marker that raises CollectionNotInitializedError) ->
        nothing to index; ensure must not call list or create (the create
        path handles new collections at birth)."""
        col, client = _make_qdrant_collection([])
        client.collection_exists.return_value = False
        col._backend._marker_exists.return_value = False

        assert col.get_all_metadata() == []
        client.list_payload_indexes.assert_not_called()
        client.create_payload_index.assert_not_called()


class TestNewCollectionIndexesAtBirth:
    def test_create_branch_builds_document_and_filter_indexes(self):
        """A brand-new collection gets the document text index AND the filter
        indexes directly -- absent by construction, no listing needed."""
        col, client = _make_qdrant_collection([])
        client.collection_exists.return_value = False

        col._ensure_remote_collection(dimension=4)

        created = _index_creates(client)
        assert (col._remote_collection, "document", "text") in created
        assert (col._remote_collection, SOURCE_INDEX_FIELD, "keyword") in created
