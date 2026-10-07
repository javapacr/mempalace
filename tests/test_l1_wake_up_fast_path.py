"""Regression guard for Layer1.generate()'s single-pass candidate fetch.

Fork history: 6cdc5e4 replaced L1's offset pagination with one
``get(limit=MAX_SCAN)`` (#2153 pattern). Upstream 0da09a2 superseded that
with the ``get_recent`` backend capability -- ORDER BY filed_at DESC LIMIT
MAX_SCAN pushed into storage where possible. This guard now pins that
contract: exactly one ``get_recent`` call, bounded by MAX_SCAN, ordered by
``filed_at``, wing-scoped when a wing is set, and no ``get()`` pagination.
"""

from mempalace.backends.base import GetResult

_FAILURE_HEADERS = ("## L1 — No palace found", "## Palace is busy")


def _make_mock_collection(docs_list, metas_list):
    """Mock collection exposing get_recent(); get() is tracked so a
    fallback to paged get() is visible."""

    class _Collection:
        def __init__(self):
            self._docs = docs_list
            self._metas = metas_list
            self.get_recent_calls = []
            self.get_calls = []

        def get_recent(self, *, limit, where=None, order_field=None, include=None):
            self.get_recent_calls.append(
                {"limit": limit, "where": where, "order_field": order_field, "include": include}
            )
            pairs = list(zip(self._docs, self._metas))
            if where:
                pairs = [p for p in pairs if all(p[1].get(k) == v for k, v in where.items())]
            if order_field:
                pairs.sort(key=lambda p: p[1].get(order_field) or "", reverse=True)
            pairs = pairs[:limit]
            return GetResult(
                ids=[f"id{i}" for i in range(len(pairs))],
                documents=[d for d, _ in pairs],
                metadatas=[m for _, m in pairs],
            )

        def get(self, **kwargs):
            self.get_calls.append(kwargs)
            return {"documents": [], "metadatas": []}

        def count(self):
            return len(self._docs)

    return _Collection()


def _patch_collection(monkeypatch, col):
    # Upstream opens with read_only=True; a 2-arg lambda would raise a
    # TypeError that generate() swallows into the "No palace found" header.
    monkeypatch.setattr(
        "mempalace.layers._get_collection",
        lambda path, create=False, read_only=False: col,
    )


def _assert_not_failure(result):
    assert not result.startswith(_FAILURE_HEADERS), result


class TestSinglePass:
    def test_calls_get_recent_exactly_once(self, monkeypatch):
        """generate() calls get_recent exactly once with limit=MAX_SCAN,
        ordered by filed_at, and never pages get()."""
        from mempalace.layers import Layer1

        col = _make_mock_collection(
            [
                "First drawer with some content here.",
                "Second drawer with other content.",
                "Third drawer with more stuff.",
            ],
            [
                {"wing": "sessions", "room": "technical", "filed_at": "2026-08-26T10:00:00"},
                {"wing": "sessions", "room": "planning", "filed_at": "2026-08-26T11:00:00"},
                {"wing": "knowledge", "room": "decisions", "filed_at": "2026-08-26T12:00:00"},
            ],
        )
        _patch_collection(monkeypatch, col)

        result = Layer1().generate()

        _assert_not_failure(result)
        assert len(col.get_recent_calls) == 1
        call = col.get_recent_calls[0]
        assert call["limit"] == Layer1.MAX_SCAN
        assert call["order_field"] == "filed_at"
        assert call["where"] is None
        assert col.get_calls == []

    def test_respects_wing_filter(self, monkeypatch):
        """When wing is set, get_recent receives the wing where filter."""
        from mempalace.layers import Layer1

        col = _make_mock_collection(
            ["Drawer 1"],
            [{"wing": "sessions", "room": "technical", "filed_at": "2026-08-26T10:00:00"}],
        )
        _patch_collection(monkeypatch, col)

        result = Layer1(wing="sessions").generate()

        _assert_not_failure(result)
        assert len(col.get_recent_calls) == 1
        assert col.get_recent_calls[0]["where"] == {"wing": "sessions"}
        assert col.get_calls == []

    def test_output_contains_l1_header_and_content(self, monkeypatch):
        """Output text contains L1 header and drawer content."""
        from mempalace.layers import Layer1

        col = _make_mock_collection(
            ["Important content about feature X.", "Decision about pricing model."],
            [
                {"wing": "sessions", "room": "technical", "filed_at": "2026-08-26T10:00:00"},
                {"wing": "sessions", "room": "planning", "filed_at": "2026-08-26T11:00:00"},
            ],
        )
        _patch_collection(monkeypatch, col)

        result = Layer1().generate()

        assert "## L1 — ESSENTIAL STORY" in result
        assert "content about feature X" in result or "pricing model" in result

    def test_respects_max_scan_limit(self, monkeypatch):
        """Only scans up to MAX_SCAN drawers, in one get_recent call."""
        from mempalace.layers import Layer1

        large_docs = [f"Drawer {i}" for i in range(3000)]
        large_metas = [
            {"wing": "a", "room": "b", "filed_at": f"2026-08-26T{i % 24:02d}:{i % 60:02d}:00"}
            for i in range(3000)
        ]
        col = _make_mock_collection(large_docs, large_metas)
        _patch_collection(monkeypatch, col)

        result = Layer1().generate()

        _assert_not_failure(result)
        assert len(col.get_recent_calls) == 1
        assert col.get_recent_calls[0]["limit"] == Layer1.MAX_SCAN
        assert col.get_calls == []

    def test_empty_palace_returns_no_memories(self, monkeypatch):
        """Empty collection returns 'No memories yet.' message."""
        from mempalace.layers import Layer1

        col = _make_mock_collection([], [])
        _patch_collection(monkeypatch, col)

        result = Layer1().generate()

        assert result == "## L1 — No memories yet."
        assert len(col.get_recent_calls) == 1

    def test_collection_open_failure_returns_no_palace_found(self, monkeypatch):
        """Collection open failure returns 'No palace found.' message."""
        from mempalace.layers import Layer1

        def _raise_on_open(path, create=False, read_only=False):
            raise RuntimeError("Collection not found")

        monkeypatch.setattr("mempalace.layers._get_collection", _raise_on_open)

        result = Layer1().generate()

        assert "## L1 — No palace found" in result
        assert "mempalace mine" in result

    def test_degrades_gracefully_on_backend_exception(self, monkeypatch):
        """get_recent and get() both raising degrades to 'No memories yet.'"""
        from mempalace.layers import Layer1

        class _FailingCollection:
            def get_recent(self, **kwargs):
                raise RuntimeError("Backend error")

            def get(self, **kwargs):
                raise RuntimeError("Backend error")

        _patch_collection(monkeypatch, _FailingCollection())

        result = Layer1().generate()

        assert result == "## L1 — No memories yet."

    def test_most_recent_first_ordering(self, monkeypatch):
        """Drawers with newer filed_at appear first in output."""
        from mempalace.layers import Layer1

        col = _make_mock_collection(
            ["Old drawer", "New drawer", "Middle drawer"],
            [
                {"wing": "a", "room": "b", "filed_at": "2026-08-26T09:00:00"},
                {"wing": "a", "room": "b", "filed_at": "2026-08-26T11:00:00"},
                {"wing": "a", "room": "b", "filed_at": "2026-08-26T10:00:00"},
            ],
        )
        _patch_collection(monkeypatch, col)

        result = Layer1().generate()

        _assert_not_failure(result)
        new_pos = result.find("New drawer")
        middle_pos = result.find("Middle drawer")
        assert new_pos >= 0 and middle_pos >= 0
        assert new_pos < middle_pos
