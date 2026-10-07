"""Guard: on qdrant the mined-set prefetch reads metadata in one iter_metadata pass.

``prefetch_mined_set`` and ``prefetch_complete_mtimes`` must stream a qdrant
collection through ``iter_metadata`` -- one continuous scroll cursor -- and
never fall back to ``count()`` plus ``get(limit, offset)`` paging, which
re-scrolls from the collection start on every page and turns a full prefetch
quadratic in palace size.

The helpers live in an exec'd fragment of ``mempalace.palace``; import them
through that package.
"""

from unittest import mock

import pytest

import mempalace.palace as palace
from mempalace.backends import qdrant as qdrant_mod
from mempalace.backends.base import PalaceRef
from mempalace.backends.qdrant import QdrantCollection, _QdrantConfig


def _point(doc_id: str, source_file: str, mtime: float) -> dict:
    return {
        "id": f"point-{doc_id}",
        "payload": {
            qdrant_mod._PAYLOAD_ID: doc_id,
            qdrant_mod._PAYLOAD_DOCUMENT: f"content for {doc_id}",
            qdrant_mod._PAYLOAD_METADATA: {
                "source_file": source_file,
                "source_mtime": mtime,
                "chunk_total": 1,
                "normalize_version": palace.NORMALIZE_VERSION,
            },
        },
        "vector": None,
    }


@pytest.fixture
def qdrant_two_pages(monkeypatch):
    """A QdrantCollection over a mocked REST client serving two scroll pages.

    ``get`` and ``count`` are forbidden: reaching either means the prefetch
    left the single ``iter_metadata`` pass.
    """
    pages = [
        ([_point("d0", "/src/a.py", 1.0)], "p2"),
        ([_point("d1", "/src/b.py", 2.0)], None),
    ]
    client = mock.MagicMock()
    client.collection_exists.return_value = True
    client.list_payload_indexes.return_value = [
        f"{qdrant_mod._PAYLOAD_METADATA}.{field}" for field in qdrant_mod._KEYWORD_PAYLOAD_FIELDS
    ]
    client.scroll_points.side_effect = lambda collection, **kwargs: (
        pages.pop(0) if pages else ([], None)
    )
    backend = mock.MagicMock()
    backend._closed = False
    backend._marker_exists.return_value = True

    def forbidden(*args, **kwargs):
        raise AssertionError("prefetch must not page through get()/count()")

    monkeypatch.setattr(QdrantCollection, "get", forbidden)
    monkeypatch.setattr(QdrantCollection, "count", forbidden)

    col = QdrantCollection(
        backend=backend,
        client=client,
        config=_QdrantConfig(url="http://localhost:6333"),
        palace=PalaceRef(id="/tmp/fake-palace", local_path="/tmp/fake-palace"),
        collection_name="mempalace",
        remote_collection="mempalace_abc123_mempalace",
    )
    return col, client


def _scroll_offsets(client) -> list:
    return [call.kwargs.get("offset") for call in client.scroll_points.call_args_list]


def test_prefetch_mined_set_is_one_scroll_pass(qdrant_two_pages):
    col, client = qdrant_two_pages

    mined = palace.prefetch_mined_set(col)

    assert mined == {"/src/a.py": 1.0, "/src/b.py": 2.0}
    assert client.scroll_points.call_count == 2
    assert _scroll_offsets(client) == [None, "p2"]


def test_prefetch_complete_mtimes_is_one_scroll_pass(qdrant_two_pages):
    col, client = qdrant_two_pages

    complete = palace.prefetch_complete_mtimes(col, ["/src/a.py", "/src/b.py"])

    assert complete == {"/src/a.py": (1.0,), "/src/b.py": (2.0,)}
    assert client.scroll_points.call_count == 2
    assert _scroll_offsets(client) == [None, "p2"]
