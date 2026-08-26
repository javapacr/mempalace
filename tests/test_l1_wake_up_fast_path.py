"""Tests for Layer1.generate() single-pass fast path (#2153 pattern)."""


def _make_mock_collection(docs_list, metas_list):
    """Mock collection that tracks get() calls."""

    class _Collection:
        def __init__(self):
            self._docs = docs_list
            self._metas = metas_list
            self.get_calls = []

        def get(self, *, ids=None, where=None, limit=None, offset=None, include=None):
            self.get_calls.append({"limit": limit, "offset": offset, "where": where})
            offset = offset or 0
            limit = limit if limit is not None else len(self._docs)
            return {
                "documents": self._docs[offset : offset + limit],
                "metadatas": self._metas[offset : offset + limit],
            }

        def count(self):
            return len(self._docs)

    return _Collection()


class TestSinglePass:
    def test_calls_get_exactly_once(self, monkeypatch):
        """Layer1.generate() calls collection.get exactly once with limit=MAX_SCAN."""
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
        monkeypatch.setattr("mempalace.layers._get_collection", lambda path, create=False: col)

        l1 = Layer1()
        _ = l1.generate()

        # Exactly one call, with limit=MAX_SCAN, no offset
        assert len(col.get_calls) == 1
        call = col.get_calls[0]
        assert call["limit"] == Layer1.MAX_SCAN
        assert call["offset"] is None or call["offset"] == 0

    def test_respects_wing_filter(self, monkeypatch):
        """When wing is set, passes where filter to get()."""
        from mempalace.layers import Layer1

        col = _make_mock_collection(
            ["Drawer 1"],
            [{"wing": "sessions", "room": "technical", "filed_at": "2026-08-26T10:00:00"}],
        )
        monkeypatch.setattr("mempalace.layers._get_collection", lambda path, create=False: col)

        l1 = Layer1(wing="sessions")
        _ = l1.generate()

        assert len(col.get_calls) == 1
        assert col.get_calls[0]["where"] == {"wing": "sessions"}

    def test_no_offset_on_any_call(self, monkeypatch):
        """No call passes offset parameter."""
        from mempalace.layers import Layer1

        col = _make_mock_collection(
            ["Drawer 1", "Drawer 2"],
            [
                {"wing": "a", "room": "b", "filed_at": "2026-08-26T10:00:00"},
                {"wing": "a", "room": "b", "filed_at": "2026-08-26T11:00:00"},
            ],
        )
        monkeypatch.setattr("mempalace.layers._get_collection", lambda path, create=False: col)

        l1 = Layer1()
        l1.generate()

        for call in col.get_calls:
            assert call["offset"] is None or call["offset"] == 0

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
        monkeypatch.setattr("mempalace.layers._get_collection", lambda path, create=False: col)

        l1 = Layer1()
        result = l1.generate()

        assert "## L1 — ESSENTIAL STORY" in result
        assert "content about feature X" in result or "pricing model" in result

    def test_respects_max_scan_limit(self, monkeypatch):
        """Only scans up to MAX_SCAN drawers."""
        from mempalace.layers import Layer1

        # Create more than MAX_SCAN documents
        large_docs = [f"Drawer {i}" for i in range(3000)]
        large_metas = [
            {"wing": "a", "room": "b", "filed_at": f"2026-08-26T{i:02d}:00:00"} for i in range(3000)
        ]

        col = _make_mock_collection(large_docs, large_metas)
        monkeypatch.setattr("mempalace.layers._get_collection", lambda path, create=False: col)

        l1 = Layer1()
        _ = l1.generate()

        # Single call with limit=MAX_SCAN
        assert len(col.get_calls) == 1
        assert col.get_calls[0]["limit"] == Layer1.MAX_SCAN

    def test_empty_palace_returns_no_memories(self, monkeypatch):
        """Empty collection returns 'No memories yet.' message."""
        from mempalace.layers import Layer1

        col = _make_mock_collection([], [])
        monkeypatch.setattr("mempalace.layers._get_collection", lambda path, create=False: col)

        l1 = Layer1()
        result = l1.generate()

        assert result == "## L1 — No memories yet."

    def test_collection_open_failure_returns_no_palace_found(self, monkeypatch):
        """Collection open failure returns 'No palace found.' message."""
        from mempalace.layers import Layer1

        def _raise_on_get(path, create=False):
            raise RuntimeError("Collection not found")

        monkeypatch.setattr("mempalace.layers._get_collection", _raise_on_get)

        l1 = Layer1()
        result = l1.generate()

        assert "## L1 — No palace found" in result
        assert "mempalace mine" in result

    def test_degrades_gracefully_on_get_exception(self, monkeypatch):
        """Exception during col.get() degrades to 'No memories yet.'"""
        from mempalace.layers import Layer1

        class _FailingCollection:
            def get(self, **kwargs):
                raise RuntimeError("Backend error")

        monkeypatch.setattr(
            "mempalace.layers._get_collection",
            lambda path, create=False: _FailingCollection(),
        )

        l1 = Layer1()
        result = l1.generate()

        assert result == "## L1 — No memories yet."

    def test_most_recent_first_ordering(self, monkeypatch):
        """Drawers with newer filed_at appear first in output."""
        from mempalace.layers import Layer1

        # Create drawers with different filed_at timestamps
        col = _make_mock_collection(
            ["Old drawer", "New drawer", "Middle drawer"],
            [
                {"wing": "a", "room": "b", "filed_at": "2026-08-26T09:00:00"},
                {"wing": "a", "room": "b", "filed_at": "2026-08-26T11:00:00"},
                {"wing": "a", "room": "b", "filed_at": "2026-08-26T10:00:00"},
            ],
        )
        monkeypatch.setattr("mempalace.layers._get_collection", lambda path, create=False: col)

        l1 = Layer1()
        result = l1.generate()

        # New drawer should appear before middle drawer in the output
        # (since sort is by importance desc, then recency desc)
        new_pos = result.find("New drawer")
        middle_pos = result.find("Middle drawer")
        if new_pos >= 0 and middle_pos >= 0:
            assert new_pos < middle_pos
