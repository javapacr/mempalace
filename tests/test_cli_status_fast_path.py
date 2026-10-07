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

    def __init__(self, metadata_list, backend=None, facet_error=None):
        self._meta = list(metadata_list)
        self._backend = backend if backend is not None else _FacetBackend()
        self._facet_error = facet_error
        self.facet_calls = []

    def count(self):
        return len(self._meta)

    def get_all_metadata(self, where=None):
        return list(self._meta)

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
        "mempalace.backends.chroma._sqlite_wing_room_counts",
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
