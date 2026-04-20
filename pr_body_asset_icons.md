## What

Each AssetType now has a distinct emoji so users can tell types apart at a glance, everywhere assets are listed — not just in dropdowns.

| Type | Icon |
|---|---|
| video | 🎬 |
| image | 🖼️ |
| webpage | 🌐 |
| stream | 📡 |
| saved_stream | 📼 |

## Why

We had this feature and it was silently removed at some point — only webpage/stream had icons left, and only inside dropdowns. The smoke tests added here make sure it doesn't happen again.

## Where

- **Assets library** — Type badge on every row now leads with the icon.
- **Schedules page** — active + expired schedule lists show the icon before the asset name; the asset dropdown option labels use a shared ASSET_ICONS JS map that replaces the duplicated `webpage` / `stream` if/else chains (two copies, now one).
- **Device default-asset dropdowns** and anywhere else `asset_label_suffix` is used — the filter now always includes the type icon, plus `(mm:ss)` when the asset has a duration.

## How

- New `asset_icon` Jinja filter backed by a single source of truth (`cms.ui._ASSET_ICONS`).
- `asset_label_suffix` reuses the same mapping.
- Shared `ASSET_ICONS` JS const injected into `schedules.html` once, consumed by both dynamic dropdown builders.

## Tests

`tests/test_asset_icons.py` (new, 34 assertions):

- Every AssetType has an icon (fails loudly if a new type ships without one)
- Filter is registered on the Jinja environment and renders via {{ a | asset_icon }}
- `assets.html`, `schedules.html` list rows, and the schedules JS map actually reference the icons — **regression guard** because this feature has gone missing before.

`tests/test_asset_label_suffix.py` updated for the new behaviour (icon is always included; image no longer returns empty).

All 34 pass locally in ~1s.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
