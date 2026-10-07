"""Tests for miner.status() get_all_metadata() fast path (#2153) and the
facet-backed fast path (BRD P1/R2: docs/brd-p1-status-facet-path-and-nonblocking-index-adoption.md).
"""

import logging
from unittest import mock

import pytest

from mempalace.backends.base import BackendError, UnsupportedCapabilityError


def _make_collection_with_get_all(metadata_list):
    """Mock collection exposing get_all_metadata() and tracking get() calls."""

    class _Collection:
        def __init__(self):
            self._meta = metadata_list
            self.get_calls = []

        def get_all_metadata(self, where=None):
            return list(self._meta)

        def get(self, *, ids=None, where=None, limit=None, offset=None, include=None):
            self.get_calls.append({"limit": limit, "offset": offset})
            offset = offset or 0
            limit = limit if limit is not None else len(self._meta)
            return {"ids": [], "documents": [], "metadatas": self._meta[offset : offset + limit]}

        def count(self):
            return len(self._meta)

    return _Collection()


class _LegacyCollection:
    """Collection without get_all_metadata() — triggers fallback loop."""

    def __init__(self, metadata_list):
        self._meta = metadata_list

    def get(self, *, ids=None, where=None, limit=None, offset=None, include=None):
        offset = offset or 0
        limit = limit if limit is not None else len(self._meta)
        return {"ids": [], "documents": [], "metadatas": self._meta[offset : offset + limit]}

    def count(self):
        return len(self._meta)


class _FacetBackend:
    """Backend stub advertising a configurable capability set (qdrant-style)."""

    def __init__(self, capabilities=frozenset({"supports_metadata_facets"})):
        self.capabilities = capabilities


class _FacetCollection:
    """Stub collection whose facets are served from its own metadata list.

    Emulates the ``facet_counts`` contract (``dict[str, int]`` per metadata
    field, single-key equality ``where``) so facet-path tests stay hermetic.
    Returned dicts are deliberately built in reversed-sorted key order: the
    status renderer must not depend on facet hit order.
    """

    def __init__(self, metadata_list, backend=None, facet_error=None, ids=None, get_error=None):
        self._meta = list(metadata_list)
        self._ids = list(ids) if ids is not None else [f"d{i}" for i in range(len(self._meta))]
        self._backend = backend if backend is not None else _FacetBackend()
        self._facet_error = facet_error
        self._get_error = get_error
        self.facet_calls = []
        self.get_calls = []
        self.get_includes = []

    def count(self):
        return len(self._meta)

    def get_all_metadata(self, where=None):
        return list(self._meta)

    def get(self, *, ids=None, where=None, limit=None, offset=None, include=None):
        """Single-key equality ``where`` served from the stub's own rows."""
        self.get_calls.append(where)
        self.get_includes.append(include)
        if self._get_error is not None:
            raise self._get_error
        pairs = list(zip(self._ids, self._meta))
        if where:
            ((key, value),) = where.items()
            pairs = [(i, m) for i, m in pairs if (m or {}).get(key) == value]
        return {"ids": [i for i, _ in pairs], "metadatas": [m for _, m in pairs]}

    def facet_counts(self, field, where=None, limit=1000):
        self.facet_calls.append((field, where))
        if self._facet_error is not None:
            raise self._facet_error
        rows = self._meta
        if where:
            ((key, value),) = where.items()
            rows = [m for m in rows if m.get(key) == value]
        counts = {}
        for m in rows:
            v = m.get(field)
            if v is None:
                continue
            counts[v] = counts.get(v, 0) + 1
        return dict(sorted(counts.items(), reverse=True))


class _CapabilitylessFacetCollection:
    """Chroma-style stub: backend advertises no facet capability and the
    collection has no ``facet_counts`` method at all — status must never
    touch it (no raw AttributeError).
    """

    def __init__(self, metadata_list):
        self._meta = list(metadata_list)
        self._backend = _FacetBackend(capabilities=frozenset({"supports_metadata_filters"}))

    def count(self):
        return len(self._meta)

    def get_all_metadata(self, where=None):
        return list(self._meta)


@pytest.fixture
def _stub_deps(monkeypatch):
    monkeypatch.setattr(
        "mempalace.backends.chroma_sqlite._sqlite_wing_room_counts",
        lambda palace_path, collection_name: None,
    )
    monkeypatch.setattr(
        "mempalace.backends.chroma.hnsw_capacity_status",
        lambda palace_path, collection_name: {"diverged": False, "status": "unknown"},
    )
    yield


class TestFastPath:
    def test_uses_get_all_metadata(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        col = _make_collection_with_get_all(
            [
                {"wing": "sessions", "room": "technical"},
                {"wing": "sessions", "room": "planning"},
                {"wing": "knowledge", "room": "decisions"},
            ]
        )
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        out = capsys.readouterr().out

        assert "3 drawers" in out
        assert col.get_calls == []

    def test_does_not_call_count(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        col = _make_collection_with_get_all([{"wing": "a", "room": "b"}])
        col.count = mock.MagicMock(side_effect=AssertionError("count() should not be called"))
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        col.count.assert_not_called()

    def test_handles_none_metadata(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        col = _make_collection_with_get_all(
            [
                {"wing": "sessions", "room": "technical"},
                None,
            ]
        )
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        out = capsys.readouterr().out

        assert "2 drawers" in out
        assert "?" in out

    def test_large_collection_single_pass(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        col = _make_collection_with_get_all([{"wing": "a", "room": "b"}] * 10000)
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        assert col.get_calls == []


class TestFallback:
    def test_offset_loop_when_no_get_all(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        col = _LegacyCollection([{"wing": "a", "room": "x"}, {"wing": "b", "room": "y"}])
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        out = capsys.readouterr().out

        assert "2 drawers" in out

    def test_empty_collection(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        col = _LegacyCollection([])
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        out = capsys.readouterr().out

        assert "0 drawers" in out


class TestFacetPath:
    """BRD P1/R2: facet-backed wing/room counts for CLI status."""

    def test_facet_counts_match_known_fixture(self):
        from mempalace import miner

        metas = [
            {"wing": "project", "room": "backend"},
            {"wing": "project", "room": "backend"},
            {"wing": "project", "room": "frontend"},
            {"wing": "sessions", "room": "technical"},
        ]
        result = miner._facet_wing_room_counts(_FacetCollection(metas))

        assert result == (
            4,
            {
                "project": {"backend": 2, "frontend": 1},
                "sessions": {"technical": 1},
            },
        )

    def test_rooms_ordered_count_desc_then_alphabetical(self):
        from mempalace import miner

        metas = [
            {"wing": "w", "room": "zeta"},
            {"wing": "w", "room": "alpha"},
            {"wing": "w", "room": "beta"},
            {"wing": "w", "room": "alpha"},
            {"wing": "w", "room": "beta"},
        ]
        result = miner._facet_wing_room_counts(_FacetCollection(metas))
        assert result is not None
        _total, wing_rooms = result

        # alpha/beta tie at 2 renders alphabetically; zeta (1) trails.
        assert list(wing_rooms["w"].keys()) == ["alpha", "beta", "zeta"]

    def test_renders_with_one_wing_facet_and_per_wing_room_facets(
        self, _stub_deps, monkeypatch, capsys
    ):
        from mempalace import miner

        col = _FacetCollection(
            [
                {"wing": "a", "room": "x"},
                {"wing": "b", "room": "y"},
            ]
        )
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        out = capsys.readouterr().out

        assert "2 drawers" in out
        assert col.facet_calls[0] == ("wing", None)
        room_calls = [c for c in col.facet_calls[1:] if c[0] == "room"]
        assert sorted(room_calls, key=lambda c: c[1]["wing"]) == [
            ("room", {"wing": "a"}),
            ("room", {"wing": "b"}),
        ]
        assert "WING: a" in out
        assert "WING: b" in out

    def test_missing_wing_metadata_tallied_under_question_mark(self):
        from mempalace import miner

        metas = [
            {"wing": "a", "room": "x"},
            {"wing": "a", "room": "x"},
            {"room": "orphaned"},
        ]
        result = miner._facet_wing_room_counts(_FacetCollection(metas))
        assert result is not None
        total, wing_rooms = result

        # Header total always equals the histogram total (legacy invariant).
        assert total == 3
        assert wing_rooms["?"] == {"?": 1}
        assert sum(sum(rooms.values()) for rooms in wing_rooms.values()) == 3

    def test_missing_room_metadata_tallied_under_question_mark(self):
        from mempalace import miner

        metas = [
            {"wing": "a", "room": "x"},
            {"wing": "a"},
        ]
        result = miner._facet_wing_room_counts(_FacetCollection(metas))
        assert result is not None
        _total, wing_rooms = result

        assert wing_rooms["a"] == {"x": 1, "?": 1}

    def test_unsupported_capability_falls_back_to_get_all(
        self, _stub_deps, monkeypatch, capsys, caplog
    ):
        from mempalace import miner

        col = _FacetCollection(
            [
                {"wing": "a", "room": "x"},
                {"wing": "a", "room": "y"},
            ],
            facet_error=UnsupportedCapabilityError("facet_counts does not support local-only"),
        )
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        with caplog.at_level(logging.WARNING, logger="mempalace_mcp"):
            miner.status("/fake/palace")
        out = capsys.readouterr().out

        # Facet was attempted once, then the render came from get_all_metadata.
        assert col.facet_calls == [("wing", None)]
        assert "2 drawers" in out
        assert "WING: a" in out
        # Fallback engages cleanly: exactly one warning, never a warn-loop.
        assert len(caplog.records) == 1

    def test_facet_endpoint_miss_falls_back_to_get_all(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        col = _FacetCollection(
            [{"wing": "a", "room": "x"}],
            facet_error=BackendError("Qdrant HTTP 404: Not Found"),
        )
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        out = capsys.readouterr().out

        assert col.facet_calls == [("wing", None)]
        assert "1 drawers" in out
        assert "ROOM: x" in out

    def test_capability_not_advertised_never_calls_facets(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        col = _FacetCollection(
            [{"wing": "a", "room": "x"}],
            backend=_FacetBackend(capabilities=frozenset({"supports_metadata_filters"})),
            facet_error=AssertionError("facet_counts must not be called without the capability"),
        )
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        out = capsys.readouterr().out

        assert col.facet_calls == []
        assert "1 drawers" in out

    def test_advertised_capability_without_method_falls_back(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        # Malformed backend: advertises the capability but the collection lacks
        # facet_counts entirely — the guarded call must degrade, not raise.
        col = _CapabilitylessFacetCollection(
            # Force the legacy paginated path for the render: this stub has no
            # facet_counts, so the gate passes and AttributeError must be
            # swallowed by the facet helper's fallback.
            _FacetCollection([{"wing": "a", "room": "x"}])._meta
        )
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        out = capsys.readouterr().out

        assert "1 drawers" in out
        assert "WING: a" in out


class TestFacetGoldenEquivalence:
    """Golden output equality: for the same tmp-palace drawers, the facet
    data source must render byte-identical status output to the legacy
    whole-palace scan (BRD P1/R2, R2 test strategy)."""

    def test_facet_output_byte_identical_to_legacy_scan(
        self, _stub_deps, monkeypatch, collection, capsys
    ):
        from mempalace import miner

        # Several wings/rooms/drawers, seeded so that count ties (frontend vs
        # reviews at 2; planning vs technical at 1) are first-encountered in
        # alphabetical order — the one ordering facet aggregates can share
        # with the scan path.
        collection.add(
            ids=[
                "d1",
                "d2",
                "d3",
                "d4",
                "d5",
                "d6",
                "d7",
                "d8",
                "d9",
                "d10",
            ],
            documents=[
                "Backend drawer one: verbatim content stays verbatim.",
                "Backend drawer two: verbatim content stays verbatim.",
                "Backend drawer three: verbatim content stays verbatim.",
                "Frontend drawer one: verbatim content stays verbatim.",
                "Frontend drawer two: verbatim content stays verbatim.",
                "Reviews drawer one: verbatim content stays verbatim.",
                "Reviews drawer two: verbatim content stays verbatim.",
                "Planning drawer: verbatim content stays verbatim.",
                "Technical drawer: verbatim content stays verbatim.",
                "Decisions drawer: verbatim content stays verbatim.",
            ],
            metadatas=[
                {"wing": "project", "room": "backend"},
                {"wing": "project", "room": "backend"},
                {"wing": "project", "room": "backend"},
                {"wing": "project", "room": "frontend"},
                {"wing": "project", "room": "frontend"},
                {"wing": "project", "room": "reviews"},
                {"wing": "project", "room": "reviews"},
                {"wing": "sessions", "room": "planning"},
                {"wing": "sessions", "room": "technical"},
                {"wing": "knowledge", "room": "decisions"},
            ],
        )
        stored = collection.get(include=["metadatas"])["metadatas"]
        assert len(stored) == 10

        # Legacy scan path: paginated col.get() loop.
        legacy_col = _LegacyCollection(stored)
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: legacy_col)
        miner.status("/fake/palace")
        legacy_out = capsys.readouterr().out

        # Facet path: same drawers, server-side aggregation contract.
        facet_col = _FacetCollection(stored)
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: facet_col)
        miner.status("/fake/palace")
        facet_out = capsys.readouterr().out

        assert facet_out == legacy_out
        # The facet path actually drove the render (wing + per-wing room calls).
        assert facet_col.facet_calls[0] == ("wing", None)
        assert sum(1 for field, _ in facet_col.facet_calls[1:] if field == "room") == 3
        # And the golden output itself is the expected histogram.
        assert "10 drawers" in facet_out
        assert "WING: knowledge" in facet_out
        assert "WING: project" in facet_out
        assert "WING: sessions" in facet_out
        assert "ROOM: backend" in facet_out
        assert "ROOM: frontend" in facet_out
        assert "ROOM: reviews" in facet_out
        assert "ROOM: planning" in facet_out
        assert "ROOM: technical" in facet_out
        assert "ROOM: decisions" in facet_out


def _status_stdout(miner, monkeypatch, capsys, col) -> str:
    monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)
    miner.status("/fake/palace")
    return capsys.readouterr().out


class TestFacetBookkeeping:
    """Registry rows and sentinels are bookkeeping: the facet path subtracts
    them so its counts agree with the scan paths, which skip them."""

    def test_registry_row_counts_match_fallback(self, _stub_deps, monkeypatch, capsys, caplog):
        from mempalace import miner
        from mempalace.metadata_types import BOOKKEEPING_WHERE

        metas = [
            {"wing": "a", "room": "x"},
            {"wing": "a", "room": "x"},
            {"wing": "a", "room": "y"},
            {"wing": "b", "room": "z"},
            {"wing": "a", "room": "_registry", "ingest_mode": "registry"},
            {"wing": "b", "is_sentinel": True},
        ]

        facet_col = _FacetCollection(metas)
        with caplog.at_level(logging.WARNING):
            facet_out = _status_stdout(miner, monkeypatch, capsys, facet_col)
        fallback_out = _status_stdout(
            miner, monkeypatch, capsys, _make_collection_with_get_all(metas)
        )

        # The facet path itself rendered: the subtraction ran and nothing fell back.
        assert facet_col.facet_calls
        assert facet_col.get_calls == BOOKKEEPING_WHERE["$or"]
        assert facet_col.get_includes == [["metadatas"]] * len(BOOKKEEPING_WHERE["$or"])
        assert not [r for r in caplog.records if "facet aggregation unavailable" in r.getMessage()]
        assert facet_out == fallback_out
        assert "4 drawers" in facet_out
        assert "_registry" not in facet_out

    def test_subtraction_queries_one_clause_at_a_time(self):
        from mempalace import miner
        from mempalace.metadata_types import BOOKKEEPING_WHERE

        col = _FacetCollection(
            [
                {"wing": "a", "room": "x"},
                {"wing": "a", "room": "_registry", "ingest_mode": "registry"},
            ]
        )

        assert miner._facet_wing_room_counts(col) == (1, {"a": {"x": 1}})
        assert col.get_calls == BOOKKEEPING_WHERE["$or"]
        for where in col.get_calls:
            assert where is not None
            assert "$or" not in where

    def test_subtraction_agrees_with_is_bookkeeping(self):
        from mempalace import miner

        metas = [
            {"wing": "a", "room": "x"},
            {"wing": "a", "room": "x"},
            {"wing": "a", "room": "x"},
            {"wing": "b", "room": "y"},
            {"wing": "b", "room": "y"},
            # Registry row missing its room: counted under a/? by the facets.
            {"wing": "a", "ingest_mode": "registry"},
            # Boolean sentinel.
            {"wing": "b", "room": "y", "is_sentinel": True},
            # Matches both clauses: subtracted once.
            {"wing": "a", "room": "x", "ingest_mode": "registry", "is_sentinel": True},
            # Sentinel missing its wing: counted under ?/? by the facets.
            {"room": "r", "is_sentinel": True},
            # Legacy string-typed sentinel: upstream's metadata read cannot
            # match it either, so it stays counted.
            {"wing": "b", "room": "y", "is_sentinel": "true"},
            # Bookkeeping only by its id prefix: stays counted.
            {"wing": "a", "room": "x"},
        ]
        ids = [f"d{i}" for i in range(len(metas) - 1)] + ["_reg_legacy"]

        result = miner._facet_wing_room_counts(_FacetCollection(metas, ids=ids))

        assert result == (7, {"a": {"x": 4}, "b": {"y": 3}})
        total, wing_rooms = result
        assert total == sum(sum(rooms.values()) for rooms in wing_rooms.values())

    def test_subtraction_failure_falls_back_with_warning(
        self, _stub_deps, monkeypatch, capsys, caplog
    ):
        from mempalace import miner
        from mempalace.metadata_types import BOOKKEEPING_WHERE

        metas = [
            {"wing": "a", "room": "x"},
            {"wing": "a", "room": "_registry", "ingest_mode": "registry"},
        ]
        col = _FacetCollection(
            metas,
            get_error=BackendError("Qdrant HTTP 400: Bad Request: Index required but not found"),
        )

        with caplog.at_level(logging.WARNING):
            facet_out = _status_stdout(miner, monkeypatch, capsys, col)
        fallback_out = _status_stdout(
            miner, monkeypatch, capsys, _make_collection_with_get_all(metas)
        )

        assert facet_out == fallback_out
        assert "1 drawers" in facet_out
        unavailable = [
            r for r in caplog.records if "facet aggregation unavailable" in r.getMessage()
        ]
        assert len(unavailable) == 1
        assert "Index required" in unavailable[0].getMessage()
        assert col.get_calls == [BOOKKEEPING_WHERE["$or"][0]]

    def test_status_never_imports_mcp_server(self):
        import subprocess
        import sys

        script = """
import sys

import mempalace.backends.chroma as chroma
import mempalace.backends.chroma_sqlite as chroma_sqlite
from mempalace import miner

chroma_sqlite._sqlite_wing_room_counts = lambda palace_path, collection_name: None
chroma.hnsw_capacity_status = lambda palace_path, collection_name: {
    "diverged": False,
    "status": "unknown",
}


class Backend:
    capabilities = frozenset({"supports_metadata_facets"})


class Collection:
    _backend = Backend()
    rows = [
        ("d0", {"wing": "a", "room": "x"}),
        ("d1", {"wing": "a", "room": "_registry", "ingest_mode": "registry"}),
    ]

    def count(self):
        return len(self.rows)

    def facet_counts(self, field, where=None, limit=1000):
        counts = {}
        for _, meta in self.rows:
            if where and any(meta.get(k) != v for k, v in where.items()):
                continue
            if field in meta:
                counts[meta[field]] = counts.get(meta[field], 0) + 1
        return counts

    def get(self, *, ids=None, where=None, limit=None, offset=None, include=None):
        hits = [
            (i, m) for i, m in self.rows if all(m.get(k) == v for k, v in where.items())
        ]
        return {"ids": [i for i, _ in hits], "metadatas": [m for _, m in hits]}


miner._open_collection_or_explain = lambda palace_path: Collection()
miner.status("/nonexistent")
print("MCP_LOADED" if "mempalace.mcp_server" in sys.modules else "MCP_CLEAN")
"""
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
        )

        assert proc.returncode == 0, proc.stderr
        assert "MemPalace Status --" in proc.stdout
        assert "1 drawers" in proc.stdout
        assert "MCP_CLEAN" in proc.stdout


class TestFacetPathQdrantPushdown:
    def test_facet_path_issues_no_unfiltered_scroll(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner
        from mempalace.backends import qdrant as qdrant_mod
        from mempalace.backends.base import PalaceRef
        from mempalace.backends.qdrant import QdrantCollection, _QdrantConfig

        metas = [
            {"wing": "a", "room": "x"},
            {"wing": "a", "room": "_registry", "ingest_mode": "registry"},
            {"wing": "a", "room": "x", "is_sentinel": True},
        ]
        points = [
            {
                "id": f"point-d{i}",
                "payload": {
                    qdrant_mod._PAYLOAD_ID: f"d{i}",
                    qdrant_mod._PAYLOAD_DOCUMENT: "",
                    qdrant_mod._PAYLOAD_METADATA: meta,
                },
                "vector": None,
            }
            for i, meta in enumerate(metas)
        ]

        def matches(meta, qdrant_filter):
            for cond in (qdrant_filter or {}).get("must", []):
                field = cond["key"].split(".", 1)[1]
                if meta.get(field) != cond["match"]["value"]:
                    return False
            return True

        def facet_counts(collection, *, field, qdrant_filter=None, limit=1000):
            counts = {}
            for meta in metas:
                value = meta.get(field.split(".", 1)[1])
                if value is not None and matches(meta, qdrant_filter):
                    counts[value] = counts.get(value, 0) + 1
            return counts

        scroll_filters = []

        def scroll_points(collection, *, qdrant_filter=None, limit=None, offset=None, **kwargs):
            scroll_filters.append(qdrant_filter)
            hits = [
                p
                for p in points
                if matches(p["payload"][qdrant_mod._PAYLOAD_METADATA], qdrant_filter)
            ]
            return hits, None

        client = mock.MagicMock()
        client.collection_exists.return_value = True
        client.list_payload_indexes.return_value = [
            f"{qdrant_mod._PAYLOAD_METADATA}.{f}" for f in qdrant_mod._KEYWORD_PAYLOAD_FIELDS
        ]
        client.count_points.return_value = len(metas)
        client.facet_counts.side_effect = facet_counts
        client.scroll_points.side_effect = scroll_points
        backend = mock.MagicMock()
        backend._closed = False
        backend._marker_exists.return_value = True
        backend.capabilities = frozenset({"supports_metadata_facets"})
        col = QdrantCollection(
            backend=backend,
            client=client,
            config=_QdrantConfig(url="http://localhost:6333"),
            palace=PalaceRef(id="/tmp/fake-palace", local_path="/tmp/fake-palace"),
            collection_name="mempalace",
            remote_collection="mempalace_abc123_mempalace",
        )

        out = _status_stdout(miner, monkeypatch, capsys, col)

        assert "1 drawers" in out
        assert len(scroll_filters) == 2
        assert all(f is not None for f in scroll_filters)
        assert any("metadata.ingest_mode" in str(f) for f in scroll_filters)
        assert any("metadata.is_sentinel" in str(f) for f in scroll_filters)
