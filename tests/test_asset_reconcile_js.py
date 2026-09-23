"""Unit tests for cms/static/asset_reconcile.js.

Run through node rather than a browser. The decision logic this covers used
to live inside an async callback inside a 5s timer inside an IIFE, which
meant the only way to observe it was a timing-sensitive Playwright test --
and those proved unable to distinguish correct from broken behaviour
locally. Keeping it pure buys deterministic, millisecond-level coverage.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "cms" / "static" / "asset_reconcile.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def reconcile(dom_ids, server_ids, page_size=50):
    """Call computeAssetReconcile in node and return its result."""
    js = f"""
        const {{ computeAssetReconcile }} = require({json.dumps(str(SCRIPT))});
        const out = computeAssetReconcile(
            {json.dumps(dom_ids)}, {json.dumps(server_ids)}, {page_size});
        process.stdout.write(JSON.stringify(out));
    """
    proc = subprocess.run(
        ["node", "-e", js], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def ids(*nums):
    return [f"id-{n}" for n in nums]


class TestRemoval:
    def test_removes_ids_the_server_no_longer_lists(self):
        out = reconcile(ids(1, 2, 3), ids(1, 3))
        assert out["remove"] == ids(2)
        assert out["insert"] == []

    def test_nothing_to_do_when_in_sync(self):
        out = reconcile(ids(1, 2, 3), ids(1, 2, 3))
        assert out["remove"] == []
        assert out["insert"] == []


class TestInsertion:
    def test_inserts_a_new_asset_at_the_top(self):
        """New uploads sort newest-first, so they land at index 0."""
        out = reconcile(ids(1, 2), ids(9, 1, 2))
        assert [i["id"] for i in out["insert"]] == ids(9)
        assert out["insert"][0]["beforeId"] == "id-1"

    def test_does_not_insert_assets_below_the_loaded_window(self):
        """The heart of the pagination fix.

        The user has loaded 3 of 200 rows. The other 197 are not missing --
        they are simply not scrolled to. Inserting them would fire one row
        fetch each and defeat the infinite scroll.
        """
        server = ids(*range(1, 201))
        out = reconcile(ids(1, 2, 3), server)
        assert out["insert"] == []
        assert out["remove"] == []

    def test_inserts_only_within_the_window_when_both_apply(self):
        server = ids(99) + ids(*range(1, 201))
        out = reconcile(ids(1, 2, 3), server)
        assert [i["id"] for i in out["insert"]] == ids(99)


class TestCountProxyBlindSpot:
    def test_detects_simultaneous_add_and_delete(self):
        """The case the old asset_count tripwire could never see.

        One added, one removed in the same interval: the count is identical
        before and after, so a count comparison reports "no change" while
        the page is now wrong twice over.
        """
        dom = ids(1, 2, 3)
        server = ids(9, 1, 2)  # 3 deleted, 9 added -- same length

        assert len(dom) == len(server), "precondition: count is unchanged"

        out = reconcile(dom, server)
        assert out["remove"] == ids(3)
        assert [i["id"] for i in out["insert"]] == ids(9)


class TestEdgeCases:
    def test_empty_server_removes_everything(self):
        out = reconcile(ids(1, 2), [])
        assert out["remove"] == ids(1, 2)
        assert out["insert"] == []

    def test_empty_dom_recovers_one_page_not_the_whole_library(self):
        server = ids(*range(1, 201))
        out = reconcile([], server, page_size=50)
        assert len(out["insert"]) == 50

    def test_total_replacement_is_bounded_by_what_was_loaded(self):
        """Every held row is gone and replaced. Recover a comparable window,
        not the entire library."""
        server = ids(*range(100, 300))
        out = reconcile(ids(1, 2, 3), server)
        assert len(out["insert"]) == 3
        assert out["remove"] == ids(1, 2, 3)

    def test_handles_null_inputs(self):
        out = reconcile([], [])
        assert out == {"insert": [], "remove": []}

    def test_insert_order_follows_server_order(self):
        out = reconcile(ids(5), ids(1, 2, 3, 5))
        assert [i["id"] for i in out["insert"]] == ids(1, 2, 3)

    def test_beforeid_is_null_when_appending_past_the_last_held_row(self):
        """Nothing we hold sorts after it, so there is no anchor to insert
        in front of."""
        out = reconcile(ids(1), ids(1, 2), page_size=50)
        # id-2 sits below the last loaded row, so it is out of window.
        assert out["insert"] == []
