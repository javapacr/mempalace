# tests/test_prefetch_single_pass.py
"""
Tests for the C9 prefetch fix (docs/brd-p1-review-findings.md) -- the
O(n^2) bulk-metadata fetch inside mempalace.palace's prefetch helpers.

`prefetch_mined_set()` and `prefetch_content_hashes()` used to drive
`collection.get(limit=1000, offset=k*1000)` in a page loop. On backends
whose get() is implemented by fully materializing a scroll and then
Python-slicing it (qdrant), every page re-scrolls (k+1)*1000 rows from
the collection start, so a full prefetch is O(n^2) in palace size --
measured ~84 min/pass-pair on the 458k-point CVP palace. The fix routes
both helpers through `collection.get_all_metadata()`, which is one
continuous cursor walk on backends that override it (#1796: qdrant,
milvus, pgvector) and the base offset loop on backends with true
server-side cursors (chroma) -- linear on both.

Covers:
  1. Prefetch semantics on the qdrant path -- multi-page mocked scroll
     feeding both helpers, exact result dicts asserted.
  2. Scroll-volume regression guard (the money test): the underlying
     scroll_points() must serve at most one linear pass of rows
     (<= n + _SCROLL_PAGE_SIZE); the old get-loop served ~n^2/2000.
  3. Must-not-call-get guard: the helpers must go through
     get_all_metadata() only -- no count(), no per-page get().
  4. Semantics preservation: the rewrite returns byte-for-byte the same
     result as a verbatim copy of the legacy count()+offset-loop
     algorithm over a mixed corpus on an honestly-paged collection.
  5. Partial-fetch swallow preserved: a scroll failure mid-pass must not
     raise out of the helpers -- they log a warning and return whatever
     was accumulated (an empty dict on the qdrant path, where the whole
     list is built inside get_all_metadata()).
"""

import logging
import sys
import types
from typing import Optional
from unittest import mock

import pytest


# ── Stub heavy deps so we can import mempalace modules in isolation ─────────
#
# Same documented approach as tests/test_qdrant_bulk_metadata_scroll.py:
# these names are only stubbed for the DURATION OF THIS MODULE's collection
# + test run, via the autouse fixture below. Mutating sys.modules at import
# time with no teardown risked an order-dependent flake: if pytest collected
# this file before something else that needed the REAL mempalace.config /
# mempalace.searcher / etc., that other test would silently get our fake
# module instead, with no error and no obvious cause. (Maintainer review on
# #1832.) The `if name not in sys.modules` guard keeps the stub a no-op for
# anything already imported for real (including this file's own module-level
# imports below).
_STUB_MODULE_NAMES = [
    "mempalace.knowledge_graph",
    "mempalace.searcher",
    "mempalace.palace_graph",
    "mempalace.config",
]


def _build_stub(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    # setattr spelling of the identical stub in
    # tests/test_qdrant_bulk_metadata_scroll.py -- same behavior, but
    # dynamic attribute assignment keeps static type checkers quiet.
    setattr(m, "KnowledgeGraph", lambda: types.SimpleNamespace())
    setattr(m, "search_memories", lambda *a, **kw: [])
    setattr(m, "traverse", lambda *a, **kw: {})
    setattr(m, "find_tunnels", lambda *a, **kw: {})
    setattr(m, "graph_stats", lambda *a, **kw: {})
    setattr(
        m,
        "MempalaceConfig",
        lambda: types.SimpleNamespace(
            palace_path="~/.mempalace/palace", collection_name="mempalace"
        ),
    )
    return m


@pytest.fixture(autouse=True)
def _stub_heavy_deps(monkeypatch):
    """Install fake modules for the stub names, restored automatically on
    teardown. See tests/test_qdrant_bulk_metadata_scroll.py for the full
    rationale behind monkeypatch.setitem vs bare sys.modules mutation."""
    for name in _STUB_MODULE_NAMES:
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, _build_stub(name))
    yield


# numpy is a real, light dependency -- imported eagerly here (not stubbed)
# so qdrant.py's own `import numpy as np` resolves to the real module both
# during this file's first import below and during every test.
import numpy  # noqa: E402,F401

from mempalace.backends.base import (  # noqa: E402
    BaseCollection,
    GetResult,
    PalaceRef,
)
from mempalace.backends import qdrant as qdrant_mod  # noqa: E402
from mempalace.backends.qdrant import QdrantCollection, _QdrantConfig  # noqa: E402
from mempalace.palace import (  # noqa: E402
    NORMALIZE_VERSION,
    _metadata_matches_extract_mode,
    prefetch_content_hashes,
    prefetch_mined_set,
)


# ---------------------------------------------------------------------------
# Shared helpers (copied from tests/test_qdrant_bulk_metadata_scroll.py,
# with _fake_point adapted to carry the metadata fields prefetch reads)
# ---------------------------------------------------------------------------


def _make_qdrant_collection(monkeypatch, scroll_pages):
    """
    Build a QdrantCollection with a mocked REST client whose scroll_points()
    returns the given pre-baked pages: list[tuple[list[dict_point], next_offset]].

    Unlike the sibling file's call-count-indexed mock, the page index resets
    whenever a page's next_offset is None (cursor exhausted) -- a real scroll
    starts a fresh cursor per pass, and the prefetch helpers legitimately
    walk TWO passes per collection (convo_miner calls both prefetch
    functions), so the mock must serve the full page list once per pass.
    """
    config = _QdrantConfig(url="http://localhost:6333")
    client = mock.MagicMock()
    call_log = []
    state = {"page": 0}

    def fake_scroll_points(
        collection, *, qdrant_filter=None, limit=4096, offset=None, with_vector=False
    ):
        call_log.append({"limit": limit, "offset": offset, "filter": qdrant_filter})
        page = scroll_pages[state["page"]]
        state["page"] += 1
        if page[1] is None:
            state["page"] = 0
        return page

    client.scroll_points.side_effect = fake_scroll_points
    client.collection_exists.return_value = True

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
    return col, call_log


def _drawer_meta(
    source_file,
    *,
    wing="w1",
    source_mtime: Optional[float] = 1000.0,
    normalize_version=NORMALIZE_VERSION,
    chunk_total: Optional[int] = None,
    content_hash: Optional[str] = None,
    extract_mode: Optional[str] = "exchange",
):
    """Metadata carrying exactly the fields the prefetch helpers read:
    source_file, wing, source_mtime, normalize_version, chunk_total,
    content_hash, extract_mode. Optional fields are OMITTED (not set to
    None) when absent, matching how real drawers look before a field
    existed: prefetch reads them with meta.get(), and
    _metadata_matches_extract_mode keys off the *missing key* for its
    legacy-compat rule."""
    meta = {
        "source_file": source_file,
        "wing": wing,
        "normalize_version": normalize_version,
    }
    if extract_mode is not None:
        meta["extract_mode"] = extract_mode
    if source_mtime is not None:
        meta["source_mtime"] = source_mtime
    if chunk_total is not None:
        meta["chunk_total"] = chunk_total
    if content_hash is not None:
        meta["content_hash"] = content_hash
    return meta


def _fake_point(doc_id: str, meta: dict) -> dict:
    return {
        "id": f"point-{doc_id}",
        "payload": {
            qdrant_mod._PAYLOAD_ID: doc_id,
            qdrant_mod._PAYLOAD_DOCUMENT: f"content for {doc_id}",
            qdrant_mod._PAYLOAD_METADATA: meta,
        },
        "vector": None,
    }


# ---------------------------------------------------------------------------
# 1. Prefetch semantics on the qdrant path (multi-page cursor scroll)
# ---------------------------------------------------------------------------


def _semantic_pages():
    """Eight realistic convo drawers across three scroll pages:
    a chunk_total-complete group SPLIT ACROSS the page-1/page-2 boundary,
    a legacy row without source_mtime/chunk_total/extract_mode, an
    incomplete 2-of-3 partial group, a stale normalize_version row, a
    general-mode row, and a duplicate content_hash under a new source_file.
    """
    return [
        (  # page 1
            [
                _fake_point(
                    "d0",
                    _drawer_meta(
                        "/a/session1.jsonl",
                        source_mtime=1000.5,
                        chunk_total=2,
                        content_hash="h1,h2",
                    ),
                ),
                _fake_point("d1", _drawer_meta("/a/legacy.txt", source_mtime=None)),
                _fake_point(
                    "d2",
                    _drawer_meta(
                        "/a/partial.jsonl",
                        source_mtime=2000.0,
                        chunk_total=3,
                        content_hash="h9",
                    ),
                ),
            ],
            "cursor-1",
        ),
        (  # page 2
            [
                _fake_point(
                    "d3",
                    _drawer_meta(
                        "/a/session1.jsonl",
                        source_mtime=1000.5,
                        chunk_total=2,
                        content_hash="h1,h2",
                    ),
                ),
                _fake_point(
                    "d4",
                    _drawer_meta(
                        "/a/partial.jsonl",
                        source_mtime=2000.0,
                        chunk_total=3,
                        content_hash="h9",
                    ),
                ),
                _fake_point(
                    "d5",
                    _drawer_meta(
                        "/a/stale.jsonl",
                        source_mtime=2500.0,
                        normalize_version=1,
                        content_hash="h7",
                    ),
                ),
            ],
            "cursor-2",
        ),
        (  # page 3
            [
                _fake_point(
                    "d6",
                    _drawer_meta(
                        "/a/general.jsonl",
                        source_mtime=3000.0,
                        content_hash="h3",
                        extract_mode="general",
                    ),
                ),
                _fake_point(
                    "d7",
                    _drawer_meta("/a/dup.jsonl", source_mtime=4000.0, content_hash="h1"),
                ),
            ],
            None,
        ),
    ]


class TestPrefetchQdrantPathSemantics:
    def test_mined_set_exact_result_across_three_cursor_pages(self, monkeypatch):
        col, call_log = _make_qdrant_collection(monkeypatch, _semantic_pages())

        mined = prefetch_mined_set(col, extract_mode="exchange")

        assert mined == {
            # Group completed by the page-2 drawer (count 2 >= chunk_total 2).
            "/a/session1.jsonl": 1000.5,
            # Legacy row: no stored mtime -> None must surface, not be absent.
            "/a/legacy.txt": None,
            # 2-of-3 partial group must be omitted (#2183), not trusted.
            # stale.jsonl filtered: normalize_version 1 < 2.
            # general.jsonl filtered: extract_mode mismatch.
            # Single drawer without chunk_total is trusted on its own:
            "/a/dup.jsonl": 4000.0,
        }
        # Cursor mechanics: one scroll call per page, walking by next-page
        # cursor -- no offset=N re-slicing of the collection start (C9).
        assert [e["offset"] for e in call_log] == [None, "cursor-1", "cursor-2"]
        assert all(e["limit"] == qdrant_mod._SCROLL_PAGE_SIZE for e in call_log)

    def test_content_hashes_exact_result_across_three_cursor_pages(self, monkeypatch):
        col, _ = _make_qdrant_collection(monkeypatch, _semantic_pages())

        hashes = prefetch_content_hashes(col, extract_mode="exchange")

        assert hashes == {
            # Comma-split multi-hash, first source_file wins:
            ("w1", "h1"): "/a/session1.jsonl",
            ("w1", "h2"): "/a/session1.jsonl",
            # Hash prefetch has NO chunk_total completeness rule (the legacy
            # algorithm never had one) -- the partial group's hash counts:
            ("w1", "h9"): "/a/partial.jsonl",
            # legacy.txt carries no content_hash -> no key.
            # h7 filtered: stale normalize_version.
            # h3 filtered: extract_mode mismatch.
            # dup.jsonl's h1 ignored: (w1, h1) already mapped, first wins.
        }

    def test_extract_mode_none_includes_all_modes(self, monkeypatch):
        """extract_mode=None (project-miner path) must not scope rows out."""
        col, _ = _make_qdrant_collection(monkeypatch, _semantic_pages())

        mined = prefetch_mined_set(col)
        hashes = prefetch_content_hashes(col)

        assert mined == {
            "/a/session1.jsonl": 1000.5,
            "/a/legacy.txt": None,
            "/a/general.jsonl": 3000.0,
            "/a/dup.jsonl": 4000.0,
            # partial.jsonl still omitted: chunk_total completeness is
            # mode-independent. stale.jsonl still filtered: the
            # normalize_version gate is mode-independent too.
        }
        assert hashes == {
            ("w1", "h1"): "/a/session1.jsonl",
            ("w1", "h2"): "/a/session1.jsonl",
            ("w1", "h9"): "/a/partial.jsonl",
            ("w1", "h3"): "/a/general.jsonl",
            # h7 absent too: stale.jsonl is filtered by the
            # mode-independent normalize_version gate (see the mined
            # assertion above) -- h3 appears because only the extract_mode
            # gate, not the version gate, was mode-scoped.
            # legacy.txt carries no content_hash -> no key in either mode.
        }


# ---------------------------------------------------------------------------
# 2. Scroll-volume regression guard (the money test)
# ---------------------------------------------------------------------------


def _linear_pass_pages(n):
    """Pre-baked scroll pages covering n synthetic points, each page sized
    to the real _SCROLL_PAGE_SIZE (except the last). Sizing the mock's
    pages at the true scroll page size keeps "sum of requested limits"
    equal to "rows served rounded up to one page" -- with tiny pages the
    sum would count requested-but-unserved rows and the bound below would
    be meaningless. Every row is a complete, trusted drawer (unique
    source_file, no chunk_total) plus its own content hash."""
    pages = []
    emitted = 0
    while emitted < n:
        take = min(qdrant_mod._SCROLL_PAGE_SIZE, n - emitted)
        points = [
            _fake_point(
                f"lin{emitted + i}",
                _drawer_meta(
                    f"/a/f{emitted + i}.jsonl",
                    source_mtime=float(emitted + i),
                    content_hash=f"lh{emitted + i}",
                ),
            )
            for i in range(take)
        ]
        next_offset = f"cursor-{len(pages)}" if emitted + take < n else None
        pages.append((points, next_offset))
        emitted += take
    return pages


class TestScrollVolumeRegression:
    # Three scroll pages: two full _SCROLL_PAGE_SIZE pages + a 2048-row tail.
    N_POINTS = qdrant_mod._SCROLL_PAGE_SIZE * 2 + 2048

    def test_mined_set_serves_one_linear_scroll_pass(self, monkeypatch):
        """
        The money test (C9). The old implementation drove
        collection.get(limit=1000, offset=k*1000) in a loop; on the qdrant
        adapter every such get() re-scrolls (k+1)*1000 rows from the
        collection start, so a full prefetch served ~n^2/2000 rows
        (~84 min/pass-pair on the 458k-point CVP palace). Routing through
        get_all_metadata() -- one continuous cursor walk -- must serve at
        most n rounded up to a single _SCROLL_PAGE_SIZE page (~14s
        projected on the same palace). Reintroducing the offset-loop
        routing, or a _scroll_all that restarts per page, blows this bound
        by orders of magnitude.
        """
        n = self.N_POINTS
        col, call_log = _make_qdrant_collection(monkeypatch, _linear_pass_pages(n))

        mined = prefetch_mined_set(col, extract_mode="exchange")

        assert len(mined) == n, "every unique-source row must surface as mined"
        total_requested = sum(entry["limit"] for entry in call_log)
        assert total_requested <= n + qdrant_mod._SCROLL_PAGE_SIZE, (
            f"prefetch_mined_set requested {total_requested} scroll rows for an "
            f"{n}-point collection -- more than one linear pass (n + "
            f"_SCROLL_PAGE_SIZE = {n + qdrant_mod._SCROLL_PAGE_SIZE}). The old "
            f"get(limit=, offset=) page loop served ~n^2/2000 rows here "
            f"(~84 min/pass-pair on the 458k-point CVP palace; the fix must "
            f"keep it ~14s). See docs/brd-p1-review-findings.md C9."
        )

    def test_content_hashes_serves_one_linear_scroll_pass(self, monkeypatch):
        """Same linear-pass bound for the second prefetch helper."""
        n = self.N_POINTS
        col, call_log = _make_qdrant_collection(monkeypatch, _linear_pass_pages(n))

        hashes = prefetch_content_hashes(col, extract_mode="exchange")

        assert len(hashes) == n, "every unique (wing, hash) row must surface"
        total_requested = sum(entry["limit"] for entry in call_log)
        assert total_requested <= n + qdrant_mod._SCROLL_PAGE_SIZE, (
            f"prefetch_content_hashes requested {total_requested} scroll rows for "
            f"an {n}-point collection -- more than one linear pass "
            f"(n + _SCROLL_PAGE_SIZE = {n + qdrant_mod._SCROLL_PAGE_SIZE}). "
            f"See docs/brd-p1-review-findings.md C9."
        )


# ---------------------------------------------------------------------------
# 3. Must-not-call-get guard on the qdrant path
# ---------------------------------------------------------------------------


class TestPrefetchRoutesThroughGetAllMetadataOnly:
    def test_qdrant_path_never_touches_get_or_count(self, monkeypatch):
        """get_all_metadata() is the only sanctioned bulk-read entry point;
        count()+get() is exactly the O(n^2) C9 pattern. Wire an exploding
        get()/count() into the collection so any regression fails loudly
        instead of silently degrading."""
        page1 = (
            [
                _fake_point(
                    "d0",
                    _drawer_meta("/a/only.jsonl", source_mtime=42.0, content_hash="h_only"),
                )
            ],
            None,
        )
        col, _ = _make_qdrant_collection(monkeypatch, [page1])
        col.get = mock.MagicMock(
            side_effect=AssertionError(
                "prefetch must not call get() -- route through get_all_metadata() (C9)"
            )
        )
        col.count = mock.MagicMock(
            side_effect=AssertionError(
                "prefetch must not call count() -- route through get_all_metadata() (C9)"
            )
        )

        mined = prefetch_mined_set(col, extract_mode="exchange")
        hashes = prefetch_content_hashes(col, extract_mode="exchange")

        assert mined == {"/a/only.jsonl": 42.0}
        assert hashes == {("w1", "h_only"): "/a/only.jsonl"}
        col.get.assert_not_called()
        col.count.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Semantics preservation vs the legacy count()+offset-loop algorithm
# ---------------------------------------------------------------------------


class _FakeOffsetPagedCollection(BaseCollection):
    """Minimal concrete collection with a real server-side offset cursor
    (copied from tests/test_qdrant_bulk_metadata_scroll.py). Simulates
    Chroma-like behavior: get(limit=, offset=) returns exactly the
    requested slice, so the inherited BaseCollection.get_all_metadata()
    default offset loop is the correct linear path here -- i.e. this is
    the "any backend that serves get() honestly" stand-in the legacy
    oracle and the rewrite must agree on."""

    def __init__(self, all_metadata):
        self._all = all_metadata
        self.get_call_count = 0

    def add(self, **kwargs):
        raise NotImplementedError

    def upsert(self, **kwargs):
        raise NotImplementedError

    def query(self, **kwargs):
        raise NotImplementedError

    def get(
        self, *, ids=None, where=None, where_document=None, limit=None, offset=None, include=None
    ):
        self.get_call_count += 1
        offset = offset or 0
        limit = limit if limit is not None else len(self._all)
        page = self._all[offset : offset + limit]
        return GetResult(ids=[], documents=[], metadatas=page, embeddings=None)

    def delete(self, **kwargs):
        raise NotImplementedError

    def count(self) -> int:
        return len(self._all)


def _legacy_prefetch_mined_set(collection, extract_mode=None):
    """VERBATIM copy of the pre-C9 prefetch_mined_set fetch+group algorithm
    (mempalace/palace.py before the get_all_metadata() rewrite): count() +
    get(limit=1000, offset=) page loop, then the chunk_total completion
    fold. Serves as the semantic oracle for
    TestSemanticsPreservedVsLegacyAlgorithm: for any collection whose
    get() serves honest slices (chroma), the rewrite must return exactly
    this dict. If the real prefetch's grouping/filter semantics ever
    change, update this copy by hand -- there is no automatic link."""
    groups: dict = {}
    try:
        total = collection.count()
        offset = 0
        while offset < total:
            batch = collection.get(limit=1000, offset=offset, include=["metadatas"])
            for meta in batch["metadatas"]:
                meta = meta or {}
                src = meta.get("source_file")
                if not src:
                    continue
                if not _metadata_matches_extract_mode(meta, extract_mode):
                    continue
                # Same default as file_already_mined: missing version == 1
                version = meta.get("normalize_version", 1)
                if version < NORMALIZE_VERSION:
                    continue
                stored_mtime = meta.get("source_mtime")
                mtime_key = float(stored_mtime) if stored_mtime is not None else None
                entry = groups.setdefault(src, {}).setdefault(
                    mtime_key, {"count": 0, "chunk_total": None}
                )
                entry["count"] += 1
                chunk_total = meta.get("chunk_total")
                if chunk_total is not None:
                    try:
                        entry["chunk_total"] = int(chunk_total)
                    except (TypeError, ValueError):
                        pass
            if not batch["ids"]:
                break
            offset += len(batch["ids"])
    except Exception:
        logging.getLogger("mempalace_mcp").warning(
            "prefetch_mined_set: partial fetch, %d source groups loaded", len(groups)
        )

    mined: dict = {}
    for src, by_mtime in groups.items():
        for mtime_key, entry in by_mtime.items():
            chunk_total = entry["chunk_total"]
            if chunk_total is None:
                # Legacy / registry: no completion marker — trust membership.
                mined[src] = mtime_key
                break
            if entry["count"] >= chunk_total:
                mined[src] = mtime_key
                break
    return mined


def _legacy_prefetch_content_hashes(collection, extract_mode=None):
    """VERBATIM copy of the pre-C9 prefetch_content_hashes algorithm. See
    _legacy_prefetch_mined_set for the oracle rationale."""
    hashes: dict = {}
    try:
        total = collection.count()
        offset = 0
        while offset < total:
            batch = collection.get(limit=1000, offset=offset, include=["metadatas"])
            for meta in batch["metadatas"]:
                meta = meta or {}
                content_hash_field = meta.get("content_hash")
                src = meta.get("source_file")
                wing = meta.get("wing")
                if not content_hash_field or not src or not wing:
                    continue
                if not _metadata_matches_extract_mode(meta, extract_mode):
                    continue
                version = meta.get("normalize_version", 1)
                if version < NORMALIZE_VERSION:
                    continue
                for content_hash in content_hash_field.split(","):
                    key = (wing, content_hash)
                    if content_hash and key not in hashes:
                        hashes[key] = src
            if not batch["ids"]:
                break
            offset += len(batch["ids"])
    except Exception:
        logging.getLogger("mempalace_mcp").warning(
            "prefetch_content_hashes: partial fetch, %d hashes loaded", len(hashes)
        )
    return hashes


def _mixed_corpus():
    """Corpus covering every branch both algorithms must agree on:
    complete chunk_total groups, mid-file partials short of chunk_total,
    legacy rows without source_mtime/chunk_total/extract_mode, stale
    normalize_version, extract_mode mismatches (general-mode and
    ingest_mode="sweep" rows), malformed chunk_total, multi-hash comma
    strings, duplicate (wing, hash) pairs under different source_files,
    a drawer with no source_file, and a literal None metadata entry
    (chroma can emit those -- the `meta = meta or {}` guard exists for it).
    """
    return [
        # Complete group: 3 drawers for one (source_file, mtime).
        {
            "source_file": "/a/complete.jsonl",
            "wing": "w1",
            "extract_mode": "exchange",
            "normalize_version": NORMALIZE_VERSION,
            "source_mtime": 1700000000.0,
            "chunk_total": 3,
            "content_hash": "hA,hB",
        },
        {
            "source_file": "/a/complete.jsonl",
            "wing": "w1",
            "extract_mode": "exchange",
            "normalize_version": NORMALIZE_VERSION,
            "source_mtime": 1700000000.0,
            "chunk_total": 3,
            "content_hash": "hA,hB",
        },
        {
            "source_file": "/a/complete.jsonl",
            "wing": "w1",
            "extract_mode": "exchange",
            "normalize_version": NORMALIZE_VERSION,
            "source_mtime": 1700000000.0,
            "chunk_total": 3,
            "content_hash": "hA,hB",
        },
        # Mid-file partial: 2 of chunk_total=3 -> omitted from the mined set;
        # its hashes still count (hash prefetch has no completeness rule).
        {
            "source_file": "/a/partial.jsonl",
            "wing": "w1",
            "extract_mode": "exchange",
            "normalize_version": NORMALIZE_VERSION,
            "source_mtime": 1700000100.0,
            "chunk_total": 3,
            "content_hash": "hC",
        },
        {
            "source_file": "/a/partial.jsonl",
            "wing": "w1",
            "extract_mode": "exchange",
            "normalize_version": NORMALIZE_VERSION,
            "source_mtime": 1700000100.0,
            "chunk_total": 3,
            "content_hash": "hC",
        },
        # Legacy row: no source_mtime, no chunk_total, no extract_mode
        # (pre-schema drawer; matches the "exchange" legacy-compat rule).
        {
            "source_file": "/a/legacy.txt",
            "wing": "w2",
            "normalize_version": NORMALIZE_VERSION,
            "content_hash": "hD",
        },
        # Stale normalize_version -> invisible to both prefetches.
        {
            "source_file": "/a/stale.jsonl",
            "wing": "w1",
            "extract_mode": "exchange",
            "normalize_version": 1,
            "source_mtime": 1700000200.0,
            "content_hash": "hE",
        },
        # extract_mode mismatch: general-mode drawer.
        {
            "source_file": "/a/general.jsonl",
            "wing": "w1",
            "extract_mode": "general",
            "normalize_version": NORMALIZE_VERSION,
            "source_mtime": 1700000300.0,
            "content_hash": "hF",
        },
        # Different producer: ingest_mode="sweep" with no extract_mode must
        # NOT match the "exchange" legacy-compat rule (#104).
        {
            "source_file": "/a/sweep.txt",
            "wing": "w1",
            "ingest_mode": "sweep",
            "normalize_version": NORMALIZE_VERSION,
            "source_mtime": 1700000400.0,
        },
        # Malformed chunk_total: int() fails, marker stays None -> trusted.
        {
            "source_file": "/a/badtotal.jsonl",
            "wing": "w1",
            "extract_mode": "exchange",
            "normalize_version": NORMALIZE_VERSION,
            "source_mtime": 1700000500.0,
            "chunk_total": "three",
            "content_hash": "hG",
        },
        # Duplicate (wing, hash) under a different source_file: first wins.
        {
            "source_file": "/a/dup_late.jsonl",
            "wing": "w1",
            "extract_mode": "exchange",
            "normalize_version": NORMALIZE_VERSION,
            "source_mtime": 1700000600.0,
            "content_hash": "hA",
        },
        # Trailing-comma hash string: the empty fragment must not create a key.
        {
            "source_file": "/a/trailingcomma.jsonl",
            "wing": "w1",
            "extract_mode": "exchange",
            "normalize_version": NORMALIZE_VERSION,
            "source_mtime": 1700000700.0,
            "content_hash": "hH,",
        },
        # Drawer with no source_file at all (registry sentinel shape).
        {
            "wing": "w1",
            "extract_mode": "exchange",
            "normalize_version": NORMALIZE_VERSION,
            "content_hash": "hI",
        },
        # chroma can emit literal None entries in metadatas.
        None,
    ]


class TestSemanticsPreservedVsLegacyAlgorithm:
    @pytest.mark.parametrize("extract_mode", [None, "exchange", "general"])
    def test_mined_set_matches_legacy_algorithm(self, extract_mode):
        col = _FakeOffsetPagedCollection(_mixed_corpus())
        assert prefetch_mined_set(col, extract_mode=extract_mode) == (
            _legacy_prefetch_mined_set(col, extract_mode=extract_mode)
        )

    @pytest.mark.parametrize("extract_mode", [None, "exchange", "general"])
    def test_content_hashes_matches_legacy_algorithm(self, extract_mode):
        col = _FakeOffsetPagedCollection(_mixed_corpus())
        assert prefetch_content_hashes(col, extract_mode=extract_mode) == (
            _legacy_prefetch_content_hashes(col, extract_mode=extract_mode)
        )

    def test_raw_collection_without_get_all_metadata_still_works(self):
        """A raw chromadb Collection exposes only count()/get() -- no
        get_all_metadata contract method. The prefetch helpers must fall
        back to the per-page get() loop for such objects (which is linear
        on those true-server-cursor backends), byte-for-byte equal to the
        legacy algorithm -- the exact path the real-chroma regression
        tests (tests/test_convo_miner.py::test_prefetch_mined_set_*)
        exercise end to end."""

        class _RawChromaLikeCollection:
            """Deliberately has NO get_all_metadata attribute whatsoever."""

            def __init__(self, all_metadata):
                self._data = all_metadata

            def count(self):
                return len(self._data)

            def get(
                self,
                *,
                ids=None,
                where=None,
                where_document=None,
                limit=None,
                offset=None,
                include=None,
            ):
                offset = offset or 0
                limit = limit if limit is not None else len(self._data)
                return {"ids": [], "metadatas": self._data[offset : offset + limit]}

        raw = _RawChromaLikeCollection(_mixed_corpus())
        assert not hasattr(raw, "get_all_metadata")

        assert prefetch_mined_set(raw) == _legacy_prefetch_mined_set(raw)
        assert prefetch_content_hashes(raw, extract_mode="exchange") == (
            _legacy_prefetch_content_hashes(raw, extract_mode="exchange")
        )


# ---------------------------------------------------------------------------
# 5. Partial-fetch swallow preserved
# ---------------------------------------------------------------------------


class TestPartialFetchSwallowPreserved:
    """The prefetch helpers' `except Exception: logger.warning(...)` swallow
    is load-bearing (a later manifest feature depends on partial results not
    raising): a scroll failure mid-pass must surface as a warning + whatever
    result was accumulated -- never an exception out of the helper. On the
    qdrant path the whole list is now built inside get_all_metadata(), so a
    raise mid-scroll yields an empty dict plus the warning."""

    @pytest.mark.parametrize(
        ("prefetch_fn", "warning_fragment"),
        [
            (prefetch_mined_set, "prefetch_mined_set: partial fetch"),
            (prefetch_content_hashes, "prefetch_content_hashes: partial fetch"),
        ],
        ids=["mined_set", "content_hashes"],
    )
    def test_scroll_failure_returns_partial_dict_with_warning(
        self, monkeypatch, caplog, prefetch_fn, warning_fragment
    ):
        good_page = (
            [
                _fake_point(
                    "d0",
                    _drawer_meta("/a/x.jsonl", source_mtime=111.0, content_hash="h0"),
                )
            ],
            "cursor-1",
        )
        col, call_log = _make_qdrant_collection(monkeypatch, [good_page, good_page])

        def raise_on_second_page(
            collection, *, qdrant_filter=None, limit=4096, offset=None, with_vector=False
        ):
            call_log.append({"limit": limit, "offset": offset, "filter": qdrant_filter})
            if len(call_log) == 1:
                return good_page
            raise RuntimeError("qdrant connection reset mid-scroll")

        # setattr keeps static type checkers quiet about the mocked client;
        # a plain function works because _scroll_all() only ever calls it.
        setattr(col._client, "scroll_points", raise_on_second_page)

        with caplog.at_level(logging.WARNING, logger="mempalace_mcp"):
            result = prefetch_fn(col, extract_mode="exchange")  # must not raise

        assert result == {}
        assert warning_fragment in caplog.text
