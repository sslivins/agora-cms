/* Asset library membership reconciliation.
 *
 * Pure decision logic, deliberately kept out of assets.html and free of any
 * DOM or fetch access, so it can be unit-tested directly by node without a
 * browser or a 5-second poll race. See tests/test_asset_reconcile_js.py.
 *
 * Why this exists
 * ---------------
 * The page used to decide "did membership change?" by comparing asset_count
 * between polls. That proxy is lossy in a way that matters: if one asset is
 * added and another deleted inside the same interval, the count is unchanged
 * and the page silently keeps a deleted row while missing a new one.
 *
 * It also could not respect pagination. The library loads 50 rows at a time,
 * so "id on the server but not in the DOM" is true for every asset the user
 * has not scrolled to yet. Treating those as insertions meant fetching a row
 * fragment for each one -- hundreds of requests on a large library.
 *
 * The fix is to reconcile against the server's *ordered* id list and only
 * insert ids that fall inside the window the user has actually loaded.
 */
(function (root) {
    'use strict';

    /**
     * Decide which rows to insert and which to remove.
     *
     * @param {string[]} domIds    ids currently rendered, in DOM order
     * @param {string[]} serverIds every visible id, in the page's sort order
     * @param {number}   pageSize  rows per page, used only to recover a
     *                             sensible window when nothing survives
     * @returns {{insert: {id: string, beforeId: string|null}[],
     *            remove: string[]}}
     *          `beforeId` is the surviving id the new row should be placed
     *          in front of, or null to append at the end of the window.
     */
    function computeAssetReconcile(domIds, serverIds, pageSize) {
        domIds = domIds || [];
        serverIds = serverIds || [];
        pageSize = pageSize || 50;

        const serverSet = new Set(serverIds);
        const domSet = new Set(domIds);

        // Anything we render that the server no longer lists is gone --
        // deleted, or moved out of this user's scope.
        const remove = domIds.filter(id => !serverSet.has(id));

        // The loaded window is the prefix of serverIds the user has scrolled
        // into. Its depth is the position of the last row we still hold.
        let lastLoadedIdx = -1;
        for (let i = 0; i < serverIds.length; i++) {
            if (domSet.has(serverIds[i])) lastLoadedIdx = i;
        }

        // Nothing survived: either the page was empty or everything we held
        // is gone. Recover a first-page-sized window rather than inserting
        // the entire library.
        if (lastLoadedIdx === -1) {
            const depth = Math.min(domIds.length || pageSize, serverIds.length);
            lastLoadedIdx = depth - 1;
        }

        const insert = [];
        for (let i = 0; i <= lastLoadedIdx && i < serverIds.length; i++) {
            const id = serverIds[i];
            if (domSet.has(id)) continue;
            // Place it ahead of the next id we already hold, so ordering
            // follows the server's sort without re-rendering the table.
            let beforeId = null;
            for (let j = i + 1; j < serverIds.length; j++) {
                if (domSet.has(serverIds[j])) { beforeId = serverIds[j]; break; }
            }
            insert.push({ id: id, beforeId: beforeId });
        }

        return { insert: insert, remove: remove };
    }

    root.computeAssetReconcile = computeAssetReconcile;

    // Node (unit tests) as well as the browser.
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { computeAssetReconcile: computeAssetReconcile };
    }
})(typeof window !== 'undefined' ? window : globalThis);
