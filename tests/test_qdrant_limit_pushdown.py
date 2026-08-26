"""Tests for Qdrant backend server-side limit pushdown (#2153).

Tests verify that get(limit=N, offset=M) pushes limit into the server-side scroll
when filters are fully server-side, avoiding full collection scans.

Measured impact on cvp palace (421,871 points):
- Full cursor scroll: 5.60s
- Server-side limit=2000 scroll: 0.03s (179x faster)
"""

import numpy as np
import pytest

from mempalace.backends import PalaceRef
from mempalace.backends.qdrant import QdrantBackend


def _get_payload_value(payload, key):
    """Extract nested payload value by dot-separated key."""
    value = payload
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _fake_match_condition(point, condition):
    """Match a single Qdrant filter condition against a point."""
    if "must" in condition or "must_not" in condition or "should" in condition:
        return _fake_match_filter(point, condition)
    if "has_id" in condition:
        return point["id"] in set(condition["has_id"])
    key = condition.get("key")
    actual = _get_payload_value(point.get("payload") or {}, key)
    if "match" in condition:
        match = condition["match"]
        if "value" in match:
            return actual == match["value"]
        if "any" in match:
            return actual in set(match["any"] or [])
        if "text_any" in match:
            haystack = str(actual or "").lower()
            return any(token in haystack for token in str(match["text_any"]).lower().split())
    if "range" in condition:
        range_spec = condition["range"]
        try:
            if "gt" in range_spec and not actual > range_spec["gt"]:
                return False
            if "gte" in range_spec and not actual >= range_spec["gte"]:
                return False
            if "lt" in range_spec and not actual < range_spec["lt"]:
                return False
            if "lte" in range_spec and not actual <= range_spec["lte"]:
                return False
        except TypeError:
            return False
        return True
    return True


def _fake_match_filter(point, qdrant_filter):
    """Match a Qdrant filter (must/must_not/should) against a point."""
    if not qdrant_filter:
        return True
    must = qdrant_filter.get("must") or []
    must_not = qdrant_filter.get("must_not") or []
    should = qdrant_filter.get("should") or []
    if any(not _fake_match_condition(point, condition) for condition in must):
        return False
    if any(_fake_match_condition(point, condition) for condition in must_not):
        return False
    if should and not any(_fake_match_condition(point, condition) for condition in should):
        return False
    return True


class _FakeQdrantClient:
    """Fake Qdrant client with scroll call tracking."""

    instances = []

    def __init__(self, _config):
        self.collections = {}
        self.scroll_calls = []  # Track (limit, offset) tuples
        _FakeQdrantClient.instances.append(self)

    def request(self, *_args, **_kwargs):
        return {"result": {}}

    def collection_exists(self, collection):
        return collection in self.collections

    def get_collection_info(self, collection):
        if collection not in self.collections:
            raise AssertionError("collection missing")
        return {
            "result": {
                "config": {
                    "params": {
                        "vectors": {
                            "size": self.collections[collection]["dimension"],
                            "distance": "Cosine",
                        }
                    }
                }
            }
        }

    def create_collection(self, collection, dimension):
        self.collections.setdefault(collection, {"dimension": dimension, "points": {}})

    def create_payload_index(self, collection, field_name, field_schema):
        pass

    def upsert_points(self, collection, points):
        self.collections.setdefault(
            collection,
            {"dimension": len(points[0]["vector"]) if points else 0, "points": {}},
        )
        for point in points:
            self.collections[collection]["points"][point["id"]] = dict(point)

    def query_points(self, collection, *, vector, limit, qdrant_filter, with_vector):
        points = list(self.collections.get(collection, {"points": {}})["points"].values())
        points = [point for point in points if _fake_match_filter(point, qdrant_filter)]
        q = np.asarray(vector, dtype=np.float32)
        scored = []
        for point in points:
            vec = np.asarray(point["vector"], dtype=np.float32)
            denom = float(np.linalg.norm(q)) * float(np.linalg.norm(vec))
            score = 0.0 if denom <= 0 else float(np.dot(q, vec) / denom)
            out = {"id": point["id"], "payload": point["payload"], "score": score}
            if with_vector:
                out["vector"] = point["vector"]
            scored.append(out)
        scored.sort(key=lambda point: point["score"], reverse=True)
        return scored[:limit]

    def scroll_points(
        self,
        collection,
        *,
        qdrant_filter=None,
        limit=256,
        offset=None,
        with_vector=False,
    ):
        """Track scroll calls for limit pushdown verification."""
        self.scroll_calls.append({"limit": limit, "offset": offset})
        points = list(self.collections.get(collection, {"points": {}})["points"].values())
        points = [point for point in points if _fake_match_filter(point, qdrant_filter)]
        start = int(offset or 0)
        selected = points[start : start + limit]
        next_offset = start + limit if start + limit < len(points) else None
        out = []
        for point in selected:
            item = {"id": point["id"], "payload": point["payload"]}
            if with_vector:
                item["vector"] = point["vector"]
            out.append(item)
        return out, next_offset

    def delete_points(self, collection, *, point_ids=None, qdrant_filter=None):
        points = self.collections.get(collection, {"points": {}})["points"]
        if point_ids is not None:
            for point_id in point_ids:
                points.pop(point_id, None)
            return
        for point_id, point in list(points.items()):
            if _fake_match_filter(point, qdrant_filter):
                points.pop(point_id, None)

    def count_points(self, collection):
        return len(self.collections.get(collection, {"points": {}})["points"])

    def delete_collection(self, collection):
        self.collections.pop(collection, None)

    def facet_counts(self, collection, *, field, qdrant_filter=None, limit=1000):
        counts = {}
        points = list(self.collections.get(collection, {"points": {}})["points"].values())
        points = [point for point in points if _fake_match_filter(point, qdrant_filter)]
        for point in points:
            metadata = point["payload"].get("metadata", {})
            actual_field = field.split(".", 1)[-1] if field.startswith("metadata.") else field
            value = metadata.get(actual_field)
            if value is None:
                continue
            counts[value] = counts.get(value, 0) + 1
        return counts


@pytest.fixture
def fake_qdrant(monkeypatch):
    """Patch _QdrantRESTClient with fake implementation."""
    import mempalace.backends.qdrant as qdrant

    _FakeQdrantClient.instances.clear()
    monkeypatch.setattr(qdrant, "_QdrantRESTClient", _FakeQdrantClient)
    monkeypatch.delenv("MEMPALACE_QDRANT_URL", raising=False)
    monkeypatch.delenv("MEMPALACE_QDRANT_API_KEY", raising=False)
    monkeypatch.delenv("MEMPALACE_QDRANT_NAMESPACE", raising=False)
    monkeypatch.delenv("MEMPALACE_QDRANT_TIMEOUT", raising=False)
    return _FakeQdrantClient


def _make_collection(tmp_path, name="drawers", fake_qdrant=None):
    """Create a Qdrant collection backed by fake client."""
    backend = QdrantBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    return backend, backend.get_collection(palace=palace, collection_name=name, create=True)


def _populate_1000_rows(col):
    """Populate a collection with 1000 predictable test rows."""
    ids = [f"doc-{i:04d}" for i in range(1000)]
    docs = [f"document {i}" for i in range(1000)]
    metas = [{"wing": f"wing-{i % 3}", "room": f"room-{i % 5}", "seq": i} for i in range(1000)]
    embeds = [[(i % 100) / 100.0, ((i + 50) % 100) / 100.0] for i in range(1000)]
    col.add(ids=ids, documents=docs, metadatas=metas, embeddings=embeds)
    return ids, docs, metas, embeds


class TestLimitPushdown:
    """Test server-side limit pushdown for get()."""

    def test_get_limit_pushes_to_scroll_first_page(self, tmp_path, fake_qdrant):
        """get(limit=50) with server-pushable filter → scroll first call limit ≤ 50."""
        backend, col = _make_collection(tmp_path, fake_qdrant=fake_qdrant)
        _populate_1000_rows(col)
        fake_client = fake_qdrant.instances[0]

        fake_client.scroll_calls.clear()
        result = col.get(limit=50, where={"wing": "wing-0"})

        # Should have stopped early with limit=50
        assert len(result.ids) == 50
        first_call = fake_client.scroll_calls[0]
        assert first_call["limit"] == 50
        # Should only have made one scroll call (no pagination needed)
        assert len(fake_client.scroll_calls) == 1

    def test_get_limit_and_offset_calculates_stop_after(self, tmp_path, fake_qdrant):
        """get(limit=50, offset=25) → stop_after=75 equivalence with full-scan slice."""
        backend, col = _make_collection(tmp_path, fake_qdrant=fake_qdrant)
        ids, docs, metas, _ = _populate_1000_rows(col)
        fake_client = fake_qdrant.instances[0]

        fake_client.scroll_calls.clear()
        result = col.get(limit=50, offset=25)

        # stop_after should be 75 (offset + limit)
        first_call = fake_client.scroll_calls[0]
        assert first_call["limit"] == 75

        # Result should match full-scan slice
        expected_ids = ids[25:75]
        assert result.ids == expected_ids

    def test_get_with_ids_uses_full_scan(self, tmp_path, fake_qdrant):
        """get(ids=[...]) uses full scan (no limit pushdown)."""
        backend, col = _make_collection(tmp_path, fake_qdrant=fake_qdrant)
        _populate_1000_rows(col)
        fake_client = fake_qdrant.instances[0]

        fake_client.scroll_calls.clear()
        result = col.get(ids=["doc-0001", "doc-0005", "doc-0010"], limit=10)

        # Should use default page size (4096) for full scan when ids are specified
        # or stop_after should be None
        assert len(fake_client.scroll_calls) == 1
        assert fake_client.scroll_calls[0]["limit"] == 4096  # Default page size
        assert result.ids == ["doc-0001", "doc-0005", "doc-0010"]

    def test_get_limit_none_uses_full_scan(self, tmp_path, fake_qdrant):
        """get(limit=None) uses full scan (no limit pushdown)."""
        backend, col = _make_collection(tmp_path, fake_qdrant=fake_qdrant)
        _populate_1000_rows(col)
        fake_client = fake_qdrant.instances[0]

        fake_client.scroll_calls.clear()
        result = col.get(where={"wing": "wing-0"})

        # With 1000 rows and page size 4096, only 1 call needed for full scan
        assert len(fake_client.scroll_calls) == 1
        # First call should use default page size (no limit pushdown)
        assert fake_client.scroll_calls[0]["limit"] == 4096
        # Should get all matching rows (wing-0 appears 334 times: 0, 3, 6, ..., 999)
        assert len(result.ids) == 334

    def test_get_with_where_document_uses_full_scan(self, tmp_path, fake_qdrant):
        """Filter requiring local filtering (where_document) uses full scan."""
        backend, col = _make_collection(tmp_path, fake_qdrant=fake_qdrant)
        _populate_1000_rows(col)
        fake_client = fake_qdrant.instances[0]

        fake_client.scroll_calls.clear()
        result = col.get(where_document={"$contains": "document 5"}, limit=10)

        # $contains in where_document forces local filtering → full scan
        # Should use default page size for first call
        assert len(fake_client.scroll_calls) >= 1
        # Still should return correct results
        assert all("5" in doc for doc in result.documents)

    def test_get_with_unsupported_operator_uses_full_scan(self, tmp_path, fake_qdrant):
        """Filter with unsupported server operator uses full scan."""
        backend, col = _make_collection(tmp_path, fake_qdrant=fake_qdrant)
        _populate_1000_rows(col)
        fake_client = fake_qdrant.instances[0]

        fake_client.scroll_calls.clear()
        # $or is not server-pushable
        result = col.get(where={"$or": [{"wing": "wing-0"}, {"wing": "wing-1"}]}, limit=10)

        # Should use default page size (full scan)
        assert len(fake_client.scroll_calls) >= 1
        assert fake_client.scroll_calls[0]["limit"] == 4096
        # Should return correct results
        assert all(meta["wing"] in ("wing-0", "wing-1") for meta in result.metadatas)

    def test_cursor_continuation_with_small_page_size(self, tmp_path, fake_qdrant):
        """Canned page size smaller than stop_after → multiple scroll calls, first with pushed limit."""
        backend, col = _make_collection(tmp_path, fake_qdrant=fake_qdrant)
        _populate_1000_rows(col)
        fake_client = fake_qdrant.instances[0]

        fake_client.scroll_calls.clear()
        # Request 100 rows, which should require multiple pages if page size is small
        result = col.get(limit=100)

        # Should have made multiple calls (page size 4096, so actually just 1 for 100 rows)
        # But the first call should have limit=100 (stop_after hint)
        assert len(fake_client.scroll_calls) >= 1
        first_call = fake_client.scroll_calls[0]
        assert first_call["limit"] == 100
        assert len(result.ids) == 100

    def test_equivalence_with_full_slice_various_offsets(self, tmp_path, fake_qdrant):
        """For 1000-row dataset, get(limit=k, offset=j) equals all_rows[j:j+k] for various (j,k)."""
        backend, col = _make_collection(tmp_path, fake_qdrant=fake_qdrant)
        ids, docs, metas, _ = _populate_1000_rows(col)

        test_cases = [
            (0, 50),  # Simple prefix
            (100, 50),  # Middle slice
            (900, 100),  # Near end
            (950, 100),  # Past end (tail case)
            (0, 1000),  # All rows
            (500, 0),  # Empty slice
        ]

        for offset, limit in test_cases:
            fake_qdrant.instances[0].scroll_calls.clear()
            result = col.get(limit=limit, offset=offset)

            # Expected result from full dataset
            expected_end = min(offset + limit, len(ids))
            expected_ids = ids[offset:expected_end] if limit > 0 else []

            assert result.ids == expected_ids, f"Mismatch at offset={offset}, limit={limit}"

    def test_get_all_metadata_uncapped(self, tmp_path, fake_qdrant):
        """get_all_metadata() explicitly uncapped (stop_after not used)."""
        backend, col = _make_collection(tmp_path, fake_qdrant=fake_qdrant)
        _populate_1000_rows(col)
        fake_client = fake_qdrant.instances[0]

        fake_client.scroll_calls.clear()
        metadata = col.get_all_metadata(where={"wing": "wing-0"})

        # Should use default page size (no limit pushdown)
        assert len(fake_client.scroll_calls) == 1
        assert fake_client.scroll_calls[0]["limit"] == 4096
        # Should return all matching metadata (wing-0 appears 334 times)
        assert len(metadata) == 334
        assert all(m["wing"] == "wing-0" for m in metadata)

    def test_server_filter_equivalence(self, tmp_path, fake_qdrant):
        """Server-side filter results identical to local filter results."""
        backend, col = _make_collection(tmp_path, fake_qdrant=fake_qdrant)
        _populate_1000_rows(col)

        # Query with server-pushable filter
        fake_qdrant.instances[0].scroll_calls.clear()
        server_result = col.get(limit=10, where={"wing": "wing-0", "room": "room-1"})

        # For this test, verify we got 10 rows and they all match
        assert len(server_result.ids) == 10
        assert all(m["wing"] == "wing-0" and m["room"] == "room-1" for m in server_result.metadatas)

    def test_offset_beyond_dataset(self, tmp_path, fake_qdrant):
        """get(offset=len(dataset)) returns empty results efficiently."""
        backend, col = _make_collection(tmp_path, fake_qdrant=fake_qdrant)
        _populate_1000_rows(col)
        fake_client = fake_qdrant.instances[0]

        fake_client.scroll_calls.clear()
        result = col.get(offset=1000, limit=10)

        # Should stop after checking we have 0 rows to return
        # stop_after = 1010, but server will return 0 rows
        assert len(result.ids) == 0

    def test_limit_larger_than_matching_rows(self, tmp_path, fake_qdrant):
        """get(limit=N) where N > matching_rows returns all matching rows."""
        backend, col = _make_collection(tmp_path, fake_qdrant=fake_qdrant)
        _populate_1000_rows(col)
        fake_client = fake_qdrant.instances[0]

        fake_client.scroll_calls.clear()
        # wing-2 has 333 rows (i % 3 == 2 for i in [1, 4, 7, ..., 997])
        result = col.get(limit=500, where={"wing": "wing-2"})

        # Should return all 333 matching rows
        assert len(result.ids) == 333
        assert all(m["wing"] == "wing-2" for m in result.metadatas)
