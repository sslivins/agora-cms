# Agora CMS — TODO

## Backlog

### NTP server for device clock sync
- [x] Add a chrony NTP container to `docker-compose.yml` — serves time on UDP 123, inherits host clock
- [x] Pi devices point `systemd-timesyncd` at CMS server (`agora-cms.local`) for proper NTP slewing
- [x] Advertise NTP service via Avahi (`_ntp._udp` in `setup.sh`)
- [x] No application code changes needed — NTP handles slewing correctly (no backward jumps)

### Security hardening
- [ ] Move device WebSocket connections to WSS (TLS) — auth tokens currently travel in plaintext over the wire. Low risk on trusted LAN but needed for internet-exposed deployments.
- [ ] Consider disabling the device-side REST API when CMS-managed — the CMS never calls it (all communication is over WebSocket), so it's extra attack surface. Could be a CMS-pushed config flag.

- [x] mDNS broadcast — CMS advertises itself via Avahi/mDNS as `agora-cms.local` (configured in setup.sh)

### Reduce `location.reload()` usage

Several pages reload the entire page after user actions or on polling changes.
Replacing these with in-place DOM updates would eliminate page flashes and feel smoother.

**Dashboard (`dashboard.html`)**
- Currently does a full `location.reload()` when the fingerprint changes (device online/offline, schedule starts/ends, temperature bucket shifts).
- 5 panels with complex computed state (countdowns, badges, temperature bucketing) — largest rewrite effort.
- The "End Now" button also reloads after success.

**Devices (`devices.html`)**
- Already does in-place updates for pipeline state + playback asset.
- Full reload on structural changes (device added/removed, status change, firmware update).
- Smart deferral: skips reload while a detail panel is expanded.
- Post-action reloads: adopt, delete, upgrade, check-for-updates all reload.

**Assets (`assets.html`)**
- Already does in-place updates for variant badges and expanded detail tables.
- Full reload when asset count changes (upload/delete) and no detail panel is open.
- Post-action reloads: upload, delete.

**Profiles (`profiles.html`)**
- Polling is fully in-place (variant badges + transcoding queue). No polling reload.
- Post-action reloads: create, edit, delete profile.

**Schedules (`schedules.html`)**
- No polling at all (admin CRUD page, nothing changes in background).
- Post-action reloads: create, edit schedule.

**No polling (fine as-is):**
- `history.html` — paginated log, no real-time data.
- `settings.html` — static config form.

**Priority order:**
1. Dashboard — most visible page, reloads most often.
2. Devices post-action — adopt/delete could update the row in-place.
3. Profiles/Assets post-action — lower frequency, lower impact.
