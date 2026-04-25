"""Geometric layout assertions for Playwright E2E tests — issue #444.

Helpers that catch the *class of bug* where UI elements visually overflow
their container or the viewport. Intentionally narrow: three primitives,
no clipped-text detector, no pixel diffs.

See sslivins/agora-cms#444 for the full design rationale.
"""

from playwright.sync_api import Locator, Page


# ── Constants ──

# Cell-vs-button overflow: tight tolerance, sub-pixel slop.
_CELL_TOL = 1.0
# Viewport-vs-element overflow: looser, headless rounding can exceed 1px.
_VIEWPORT_TOL = 3.0
# Open-kebab anchor distance: how far menu's nearest horizontal edge can
# sit from the trigger center before we call it un-anchored. positionKebab()
# right-aligns by default; on small viewports it clamps to viewport - 8px.
_ANCHOR_TOL = 64.0


# ── Page-level overflow ──

def assert_no_horizontal_overflow(page: Page, *, label: str = "") -> None:
    """Page document must not extend past the viewport horizontally.

    Catches the canonical "table is wider than the page" bug. Uses
    ``clientWidth`` so scrollbar gutters don't cause false negatives.
    Compares against the larger of ``html`` / ``body`` ``scrollWidth``
    because some layouts overflow on body, others on html.
    """
    metrics = page.evaluate("""() => ({
        scrollW: Math.max(
            document.documentElement.scrollWidth,
            document.body ? document.body.scrollWidth : 0,
        ),
        clientW: document.documentElement.clientWidth,
        innerW: window.innerWidth,
    })""")
    overflow = metrics["scrollW"] - metrics["clientW"]
    assert overflow <= _VIEWPORT_TOL, (
        f"horizontal overflow {label}: scrollWidth={metrics['scrollW']} "
        f"clientWidth={metrics['clientW']} innerWidth={metrics['innerW']} "
        f"(overflow={overflow}px, tol={_VIEWPORT_TOL}px)"
    )


# ── Closed-kebab cell containment ──

def assert_closed_kebabs_in_cells(
    page: Page,
    container: Locator,
    *,
    label: str = "",
) -> None:
    """Every closed ``.btn-kebab`` inside ``container`` must stay within
    its own ``td``/``th`` cell horizontally.

    Catches the "kebab visually pokes past the right edge of the table"
    regression. Uses ``closest('td,th')`` so the helper is generic across
    tables that wrap cell content in extra divs.
    """
    pairs = page.evaluate(
        """(root) => {
            const cells = [];
            for (const btn of root.querySelectorAll('.btn-kebab')) {
                const cell = btn.closest('td,th');
                if (!cell) continue;
                const b = btn.getBoundingClientRect();
                const c = cell.getBoundingClientRect();
                cells.push({
                    btnLeft: b.left, btnRight: b.right,
                    cellLeft: c.left, cellRight: c.right,
                });
            }
            return cells;
        }""",
        container.element_handle(),
    )
    assert pairs, f"no .btn-kebab found inside container {label}"
    for i, p in enumerate(pairs):
        right_overflow = p["btnRight"] - p["cellRight"]
        left_overflow = p["cellLeft"] - p["btnLeft"]
        assert right_overflow <= _CELL_TOL, (
            f"kebab #{i} {label} overflows its cell on the right: "
            f"btn.right={p['btnRight']} cell.right={p['cellRight']} "
            f"(overflow={right_overflow}px, tol={_CELL_TOL}px)"
        )
        assert left_overflow <= _CELL_TOL, (
            f"kebab #{i} {label} overflows its cell on the left: "
            f"btn.left={p['btnLeft']} cell.left={p['cellLeft']} "
            f"(overflow={left_overflow}px, tol={_CELL_TOL}px)"
        )


# ── Open-kebab viewport containment ──

def assert_open_kebab_in_viewport(
    page: Page,
    kebab_button: Locator,
    *,
    label: str = "",
) -> None:
    """Open the given ``.btn-kebab`` and assert the menu is fully in
    viewport and anchored near the trigger.

    Waits via ``wait_for_function`` for the menu to (a) be in
    ``:popover-open`` state and (b) have moved off its parked
    ``-9999px`` position before measuring — avoids flake from
    measuring before ``positionKebab()`` runs.
    """
    btn_box = kebab_button.bounding_box()
    assert btn_box is not None, f"kebab trigger {label} has no bounding box"

    kebab_button.click()
    # Wait for the popover to open AND for positionKebab() to lift it
    # out of the parked off-screen position.
    page.wait_for_function(
        """() => {
            const m = document.querySelector('.kebab-menu:popover-open');
            if (!m) return false;
            const r = m.getBoundingClientRect();
            return r.top > -1000 && r.left > -1000;
        }""",
        timeout=5000,
    )

    # Re-read the trigger's position AFTER the click. If the trigger
    # lives inside an ``overflow-x: auto`` ancestor (e.g. a
    # ``.table-wrap`` on /devices), Playwright auto-scrolls the
    # ancestor to bring the trigger into view as part of ``click()``
    # — so the pre-click ``btn_box`` is stale by the time the menu
    # anchors. Read fresh.
    btn_box_post = kebab_button.bounding_box()
    assert btn_box_post is not None, (
        f"kebab trigger {label} has no bounding box after click"
    )
    btn_center_x = btn_box_post["x"] + btn_box_post["width"] / 2

    metrics = page.evaluate("""() => {
        const m = document.querySelector('.kebab-menu:popover-open');
        const r = m.getBoundingClientRect();
        return {
            left: r.left, right: r.right, top: r.top, bottom: r.bottom,
            innerW: window.innerWidth, innerH: window.innerHeight,
        };
    }""")

    # Viewport containment (looser tolerance for fractional rounding).
    for side, val, limit in (
        ("left",   -metrics["left"],                              _VIEWPORT_TOL),
        ("top",    -metrics["top"],                               _VIEWPORT_TOL),
        ("right",  metrics["right"]  - metrics["innerW"],         _VIEWPORT_TOL),
        ("bottom", metrics["bottom"] - metrics["innerH"],         _VIEWPORT_TOL),
    ):
        assert val <= limit, (
            f"open kebab menu {label} overflows viewport on {side}: "
            f"rect=(L={metrics['left']},T={metrics['top']},"
            f"R={metrics['right']},B={metrics['bottom']}) "
            f"viewport=({metrics['innerW']}x{metrics['innerH']}) "
            f"(overflow={val}px, tol={limit}px)"
        )

    # Anchor proximity — distance from trigger center to *nearest*
    # horizontal edge of the menu. positionPopover() may right- or
    # left-align depending on viewport space; we don't pin a side.
    edge_dist = min(
        abs(metrics["left"]  - btn_center_x),
        abs(metrics["right"] - btn_center_x),
    )
    assert edge_dist <= _ANCHOR_TOL, (
        f"open kebab menu {label} not anchored near trigger: "
        f"trigger center x={btn_center_x:.0f}, menu edges "
        f"L={metrics['left']:.0f} R={metrics['right']:.0f}, "
        f"nearest distance={edge_dist:.0f}px (tol={_ANCHOR_TOL}px)"
    )

    # Restore page state for the next assertion.
    page.keyboard.press("Escape")
