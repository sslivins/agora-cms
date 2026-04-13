/* Agora CMS — client-side JavaScript */

// ── Clipboard helper (works on non-HTTPS / non-localhost) ──
function copyToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(text).then(() => showToast("Copied to clipboard"));
    }
    // Fallback for insecure contexts (HTTP + non-localhost)
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try {
        document.execCommand("copy");
        showToast("Copied to clipboard");
    } catch {
        showToast("Copy failed — select and copy manually", true);
    }
    document.body.removeChild(ta);
}

// ── Modal guard for auto-refresh polling ──
function isModalOpen() {
    return !!document.querySelector(".modal-overlay");
}

// ── Modal confirm (replaces native confirm()) ──
function showConfirm(message) {
    return new Promise((resolve) => {
        const overlay = document.createElement("div");
        overlay.className = "modal-overlay";
        const box = document.createElement("div");
        box.className = "modal-box";
        const msg = document.createElement("p");
        msg.textContent = message;
        const actions = document.createElement("div");
        actions.className = "modal-actions";
        const cancelBtn = document.createElement("button");
        cancelBtn.className = "btn btn-secondary";
        cancelBtn.textContent = "Cancel";
        const okBtn = document.createElement("button");
        okBtn.className = "btn btn-danger";
        okBtn.textContent = "Confirm";
        actions.appendChild(cancelBtn);
        actions.appendChild(okBtn);
        box.appendChild(msg);
        box.appendChild(actions);
        overlay.appendChild(box);
        document.body.appendChild(overlay);
        const close = (result) => { overlay.remove(); resolve(result); };
        cancelBtn.onclick = () => close(false);
        okBtn.onclick = () => close(true);
        overlay.addEventListener("click", (e) => { if (e.target === overlay) close(false); });
    });
}

// ── Prompt modal (replaces native prompt()) ──
function showPrompt(message, defaultValue = "", isPassword = false) {
    return new Promise((resolve) => {
        const overlay = document.createElement("div");
        overlay.className = "modal-overlay";
        const box = document.createElement("div");
        box.className = "modal-box";
        const msg = document.createElement("p");
        msg.textContent = message;
        const input = document.createElement("input");
        input.type = isPassword ? "password" : "text";
        input.className = "modal-input";
        input.value = defaultValue;
        const actions = document.createElement("div");
        actions.className = "modal-actions";
        const cancelBtn = document.createElement("button");
        cancelBtn.className = "btn btn-secondary";
        cancelBtn.textContent = "Cancel";
        const okBtn = document.createElement("button");
        okBtn.className = "btn btn-primary";
        okBtn.textContent = "OK";
        actions.appendChild(cancelBtn);
        actions.appendChild(okBtn);
        box.appendChild(msg);
        box.appendChild(input);
        box.appendChild(actions);
        overlay.appendChild(box);
        document.body.appendChild(overlay);
        input.focus();
        const close = (val) => { overlay.remove(); resolve(val); };
        cancelBtn.onclick = () => close(null);
        okBtn.onclick = () => close(input.value);
        input.addEventListener("keydown", (e) => { if (e.key === "Enter") close(input.value); });
        overlay.addEventListener("click", (e) => { if (e.target === overlay) close(null); });
    });
}

// ── Toast notification (replaces native alert()) ──
function showToast(message, isError = false) {
    const el = document.createElement("div");
    el.className = "toast" + (isError ? " toast-error" : "");
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3000);
}

// ── Formatters (run on page load) ──
function humanStorage(mb) {
    if (mb >= 1024) return (mb / 1024).toFixed(1) + " GB";
    return mb + " MB";
}

function timeAgo(dateStr) {
    const now = new Date();
    const then = new Date(dateStr);
    if (isNaN(then)) return "—";
    const seconds = Math.floor((now - then) / 1000);
    if (seconds < 60) return "less than a minute ago";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes === 1 ? "1 minute ago" : minutes + " minutes ago";
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours === 1 ? "1 hour ago" : hours + " hours ago";
    const days = Math.floor(hours / 24);
    if (days < 30) return days === 1 ? "1 day ago" : days + " days ago";
    const months = Math.floor(days / 30);
    return months === 1 ? "1 month ago" : months + " months ago";
}

document.addEventListener("DOMContentLoaded", () => {
    // Storage percentage in summary rows
    document.querySelectorAll("[data-storage-pct]").forEach(el => {
        const cap = parseInt(el.dataset.storageMb);
        const used = parseInt(el.dataset.usedMb || "0");
        if (cap > 0) {
            const pct = Math.round((cap - used) / cap * 100);
            el.textContent = pct + "% free";
        }
    });
    // Storage detail (used / capacity in human form)
    document.querySelectorAll("[data-storage-detail]").forEach(el => {
        const used = parseInt(el.dataset.usedMb || "0");
        const cap = parseInt(el.dataset.storageMb);
        el.textContent = humanStorage(used) + " used / " + humanStorage(cap) + " total";
    });
    // Last seen as human-readable time-ago
    document.querySelectorAll("[data-last-seen]").forEach(el => {
        el.textContent = timeAgo(el.dataset.lastSeen);
    });
    // Legacy UTC formatters (dashboard etc)
    document.querySelectorAll("[data-utc]").forEach(el => {
        const d = new Date(el.dataset.utc);
        if (isNaN(d)) return;
        el.textContent = d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
            + " " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", hour12: true });
    });
    // Legacy storage formatters (other pages)
    document.querySelectorAll("[data-storage-mb]:not([data-storage-pct]):not([data-storage-detail])").forEach(el => {
        const used = parseInt(el.dataset.usedMb || "0");
        const cap = parseInt(el.dataset.storageMb);
        el.textContent = used ? humanStorage(used) + " / " + humanStorage(cap) : humanStorage(cap);
    });
    // Playback position (ms → h:mm:ss)
    document.querySelectorAll("[data-position-ms]").forEach(el => {
        const ms = parseInt(el.dataset.positionMs);
        if (isNaN(ms) || ms < 0) return;
        const totalSec = Math.floor(ms / 1000);
        const h = Math.floor(totalSec / 3600);
        const m = Math.floor((totalSec % 3600) / 60);
        const s = totalSec % 60;
        const pad = n => String(n).padStart(2, "0");
        el.textContent = h > 0 ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
    });
});

// ── Device expand/collapse ──
function toggleDevice(row) {
    const deviceId = row.dataset.deviceId;
    const detail = row.nextElementSibling;
    if (!detail || detail.dataset.detailFor !== deviceId) return;
    const isOpen = row.classList.contains("expanded");
    if (isOpen) {
        row.classList.remove("expanded");
        detail.style.display = "none";
    } else {
        row.classList.add("expanded");
        detail.style.display = "";
        if (typeof applyAssetTooltips === "function") requestAnimationFrame(applyAssetTooltips);
    }
}

function toggleAsset(row) {
    const assetId = row.dataset.assetId;
    const detail = document.querySelector(`tr.asset-detail[data-detail-for="${assetId}"]`);
    if (!detail) return;
    const isOpen = row.classList.contains("expanded");
    if (isOpen) {
        row.classList.remove("expanded");
        detail.style.display = "none";
    } else {
        row.classList.add("expanded");
        detail.style.display = "";
    }
}

// ── API helpers ──
async function apiCall(method, url, body = null) {
    const opts = { method, headers: {} };
    if (body) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    if (resp.status === 401) {
        window.location.href = "/login";
        return null;
    }
    return resp;
}

// ── Device actions ──
function isDevicePlaying(deviceId) {
    const row = document.querySelector(`tr.device-row[data-device-id="${deviceId}"]`);
    return row && row.dataset.playing === "true";
}

async function renameDevice(deviceId, newName) {
    const resp = await apiCall("PATCH", `/api/devices/${deviceId}`, { name: newName.trim() });
    if (resp && resp.ok) showToast("Device renamed");
    else showToast("Rename failed", true);
}

async function assignGroup(deviceId, groupId) {
    const resp = await apiCall("PATCH", `/api/devices/${deviceId}`, { group_id: groupId || null });
    if (resp && resp.ok) showToast("Group updated");
    else showToast("Group update failed", true);
}

async function setDefaultAsset(deviceId, assetId, selectEl) {
    if (isDevicePlaying(deviceId)) {
        if (!await showConfirm("This device is currently playing.\n\nChanging the default asset will interrupt playback. Continue?")) {
            if (selectEl) selectEl.value = selectEl.dataset.prev || "";
            return;
        }
    }
    const resp = await apiCall("PATCH", `/api/devices/${deviceId}`, { default_asset_id: assetId || null });
    if (resp && resp.ok) showToast("Default asset updated");
    else showToast("Update failed", true);
}

async function setProfile(deviceId, profileId) {
    const resp = await apiCall("PATCH", `/api/devices/${deviceId}`, { profile_id: profileId || null });
    if (resp && resp.ok) showToast("Profile updated");
    else showToast("Profile update failed", true);
}

async function setDeviceTimezone(deviceId, tz) {
    const resp = await apiCall("PATCH", `/api/devices/${deviceId}`, { timezone: tz || null });
    if (resp && resp.ok) showToast("Timezone updated");
    else showToast("Timezone update failed", true);
}

async function setGroupDefaultAsset(groupId, assetId) {
    const resp = await apiCall("PATCH", `/api/devices/groups/${groupId}`, { default_asset_id: assetId || null });
    if (resp && resp.ok) showToast("Group default asset updated");
    else showToast("Update failed", true);
}

async function adoptDevice(deviceId, deviceName) {
    const resp = await apiCall("POST", `/api/devices/${deviceId}/adopt`);
    if (resp && resp.ok) location.reload();
    else if (resp) {
        const err = await resp.json().catch(() => null);
        showToast(err?.detail || "Failed to adopt device", true);
    }
}

async function deleteDevice(deviceId) {
    let msg = "Delete this device? Any schedules targeting only this device will also be removed.";
    if (isDevicePlaying(deviceId)) msg = "This device is currently playing.\n\n" + msg;
    if (!await showConfirm(msg)) return;
    const resp = await apiCall("DELETE", `/api/devices/${deviceId}`);
    if (resp && resp.ok) location.reload();
}

async function changeDevicePassword(deviceId, deviceName) {
    const password = await showPrompt("New web UI password for \"" + deviceName + "\":", "", true);
    if (password === null) return;
    if (password.length < 4) { showToast("Password must be at least 4 characters", true); return; }
    const resp = await apiCall("POST", `/api/devices/${deviceId}/password`, { password });
    if (resp && resp.ok) showToast("Device password updated");
    else if (resp) {
        const err = await resp.json().catch(() => null);
        showToast(err?.detail || "Failed to update password", true);
    }
}

async function rebootDevice(deviceId, deviceName) {
    let msg;
    if (isDevicePlaying(deviceId)) {
        msg = "\u26A0\uFE0F Device \"" + deviceName + "\" is currently playing. Are you sure you want to reboot it?";
    } else {
        msg = "Reboot device \"" + deviceName + "\"?";
    }
    if (!await showConfirm(msg)) return;
    const resp = await apiCall("POST", `/api/devices/${deviceId}/reboot`);
    if (resp && resp.ok) showToast("Reboot command sent to " + deviceName);
    else if (resp) {
        const err = await resp.json().catch(() => null);
        showToast(err?.detail || "Failed to reboot device", true);
    }
}

async function toggleSsh(deviceId, enabled) {
    const action = enabled ? "enable" : "disable";
    if (!await showConfirm("Are you sure you want to " + action + " SSH on this device?")) return;
    const resp = await apiCall("POST", `/api/devices/${deviceId}/ssh`, { enabled });
    if (resp && resp.ok) {
        showToast("SSH " + action + "d");
        location.reload();
    } else if (resp) {
        const err = await resp.json().catch(() => null);
        showToast(err?.detail || "Failed to " + action + " SSH", true);
    }
}

async function upgradeDevice(deviceId, deviceName) {
    let msg;
    if (isDevicePlaying(deviceId)) {
        msg = "\u26A0\uFE0F Device \"" + deviceName + "\" is currently playing. Are you sure you want to upgrade it?\n\nThe device will update its software and reboot.";
    } else {
        msg = "Upgrade device \"" + deviceName + "\"?\n\nThe device will update its software and reboot.";
    }
    if (!await showConfirm(msg)) return;
    // Disable this device's upgrade button to prevent double-clicks
    const btn = document.querySelector(`[onclick*="upgradeDevice('${deviceId}'"]`);
    if (btn) { btn.disabled = true; btn.textContent = 'Upgrading…'; }
    const resp = await apiCall("POST", `/api/devices/${deviceId}/upgrade`);
    if (resp && resp.ok) showToast("Upgrade command sent to " + deviceName);
    else if (resp) {
        const err = await resp.json().catch(() => null);
        showToast(err?.detail || "Failed to upgrade device", true);
        // Re-enable on failure
        if (btn) { btn.disabled = false; btn.textContent = 'Update'; }
    }
}

async function factoryResetDevice(deviceId, deviceName) {
    const msg = "\u26A0\uFE0F FACTORY RESET device \"" + deviceName + "\"?\n\n"
        + "This will wipe ALL data (assets, schedules, Wi-Fi credentials) "
        + "and the device will reboot into AP mode.\n\n"
        + "You will need physical access to the device to set it up again.";
    if (!await showConfirm(msg)) return;
    const resp = await apiCall("POST", `/api/devices/${deviceId}/factory-reset`);
    if (resp && resp.ok) showToast("Factory reset command sent to " + deviceName);
    else if (resp) {
        const err = await resp.json().catch(() => null);
        showToast(err?.detail || "Failed to factory reset device", true);
    }
}

async function toggleLocalApi(deviceId, enabled) {
    const action = enabled ? "enable" : "disable";
    if (!await showConfirm("Are you sure you want to " + action + " the local REST API on this device?")) return;
    const resp = await apiCall("POST", `/api/devices/${deviceId}/local-api`, { enabled });
    if (resp && resp.ok) {
        showToast("Local API " + action + "d");
        location.reload();
    } else if (resp) {
        const err = await resp.json().catch(() => null);
        showToast(err?.detail || "Failed to " + action + " local API", true);
    }
}

// ── Group actions ──
async function createGroup() {
    const name = document.getElementById("group-name").value.trim();
    if (!name) return;
    const desc = document.getElementById("group-desc").value.trim();
    const resp = await apiCall("POST", "/api/devices/groups/", { name, description: desc });
    if (resp && resp.ok) location.reload();
}

async function deleteGroup(groupId) {
    if (!await showConfirm("Delete this group?")) return;
    const resp = await apiCall("DELETE", `/api/devices/groups/${groupId}`);
    if (resp && resp.ok) location.reload();
}

// ── Asset actions ──
function previewAsset(assetId, filename, assetType) {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    const box = document.createElement("div");
    box.className = "modal-box preview-box";
    const header = document.createElement("div");
    header.className = "preview-header";
    const title = document.createElement("span");
    title.textContent = filename;
    const closeBtn = document.createElement("button");
    closeBtn.className = "btn btn-secondary btn-sm";
    closeBtn.textContent = "Close";
    closeBtn.onclick = () => overlay.remove();
    header.appendChild(title);
    header.appendChild(closeBtn);
    box.appendChild(header);

    const url = `/api/assets/${assetId}/preview`;
    if (assetType === "video") {
        const video = document.createElement("video");
        video.src = url;
        video.controls = true;
        video.autoplay = true;
        video.className = "preview-media";
        box.appendChild(video);
    } else {
        const img = document.createElement("img");
        img.src = url;
        img.className = "preview-media";
        box.appendChild(img);
    }

    overlay.appendChild(box);
    document.body.appendChild(overlay);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
    document.addEventListener("keydown", function esc(e) { if (e.key === "Escape") { overlay.remove(); document.removeEventListener("keydown", esc); } });
}

async function deleteAsset(assetId, filename) {
    if (!await showConfirm("Delete \"" + (filename || "this asset") + "\"?")) return;
    const resp = await apiCall("DELETE", `/api/assets/${assetId}`);
    if (resp && resp.ok) location.reload();
    else if (resp) {
        const err = await resp.json().catch(() => null);
        showToast(err?.detail || "Delete failed", true);
    }
}

async function uploadAsset(form) {
    const fileInput = document.getElementById("file-input");
    if (!fileInput || !fileInput.files.length) return false;

    const statusEl = document.getElementById("upload-status");
    const submitBtn = form.querySelector("button[type=submit]");
    const data = new FormData(form);

    submitBtn.disabled = true;
    statusEl.textContent = "Uploading… 0%";
    statusEl.className = "form-status";

    return new Promise((resolve) => {
        const xhr = new XMLHttpRequest();
        xhr.upload.addEventListener("progress", (e) => {
            if (e.lengthComputable) {
                const pct = Math.round((e.loaded / e.total) * 100);
                statusEl.textContent = "Uploading… " + pct + "%";
            }
        });
        xhr.addEventListener("load", () => {
            if (xhr.status === 401) {
                window.location.href = "/login";
            } else if (xhr.status >= 200 && xhr.status < 300) {
                statusEl.textContent = "Upload complete!";
                statusEl.className = "form-status text-success";
                setTimeout(() => location.reload(), 500);
            } else {
                try {
                    const err = JSON.parse(xhr.responseText);
                    showToast(err.detail || "Upload failed", true);
                } catch (_) {
                    showToast("Upload failed", true);
                }
                statusEl.textContent = "";
                submitBtn.disabled = false;
            }
            resolve(false);
        });
        xhr.addEventListener("error", () => {
            showToast("Upload failed — network error", true);
            statusEl.textContent = "";
            submitBtn.disabled = false;
            resolve(false);
        });
        // Build group_ids query param from selected badges
        const ids = _getUploadGroupIds();
        const globalCb = document.getElementById("upload-global");
        let qs = "";
        if (globalCb && globalCb.checked) {
            // No group_ids → admin upload becomes global
        } else if (ids.length) {
            qs = "?group_ids=" + ids.join(",");
        }
        xhr.open("POST", "/api/assets/upload" + qs);
        xhr.send(data);
    });
}

// ── Upload multi-group selector ──
function _getUploadGroupIds() {
    const badges = document.querySelectorAll("#upload-groups-badges .badge[data-group-id]");
    return Array.from(badges).map(b => b.dataset.groupId);
}

function pickUploadGroup(gid, name) {
    const container = document.getElementById("upload-groups-badges");
    if (!container || container.querySelector(`.badge[data-group-id="${gid}"]`)) return;
    const plusBtn = container.querySelector(".group-picker-wrap");
    const badge = document.createElement("span");
    badge.className = "badge badge-processing";
    badge.dataset.groupId = gid;
    badge.innerHTML = `${name} <button class="btn-x" type="button" onclick="removeUploadGroup(this.parentElement)">&times;</button>`;
    container.insertBefore(badge, plusBtn);
    // Hide the option in the popup so it can't be picked twice
    const popup = document.getElementById("upload-group-popup");
    if (popup) {
        const btn = popup.querySelector(`[data-group-id="${gid}"]`);
        if (btn) btn.style.display = "none";
        _syncPlusButton(popup);
    }
    closeAllGroupPopups();
}

function removeUploadGroup(badge) {
    const gid = badge.dataset.groupId;
    badge.remove();
    // Re-show the option in the popup
    const popup = document.getElementById("upload-group-popup");
    if (popup && gid) {
        const btn = popup.querySelector(`[data-group-id="${gid}"]`);
        if (btn) btn.style.display = "";
        _syncPlusButton(popup);
    }
}

function toggleUploadGlobal(cb) {
    const badges = document.getElementById("upload-groups-badges");
    if (cb.checked) {
        if (badges) badges.style.opacity = "0.4";
        if (badges) badges.style.pointerEvents = "none";
    } else {
        if (badges) badges.style.opacity = "1";
        if (badges) badges.style.pointerEvents = "";
    }
}

// ── Group popup (shared for upload + detail row) ──
function openGroupPopup(popupId) {
    closeAllGroupPopups();
    const popup = document.getElementById(popupId);
    if (!popup) return;
    popup.style.display = "flex";
    // Close when clicking outside (deferred to next frame to avoid catching the opening click)
    requestAnimationFrame(() => {
        function onClickOutside(e) {
            const wrap = popup.closest(".group-picker-wrap");
            if (wrap && wrap.contains(e.target)) return;
            if (popup.contains(e.target)) return;
            popup.style.display = "none";
            document.removeEventListener("click", onClickOutside, true);
            document.removeEventListener("mousedown", onClickOutside, true);
        }
        document.addEventListener("click", onClickOutside, true);
        document.addEventListener("mousedown", onClickOutside, true);
    });
}

function closeAllGroupPopups() {
    document.querySelectorAll(".group-popup").forEach(p => p.style.display = "none");
}

// Disable the + button when no visible popup items remain; re-enable otherwise
function _syncPlusButton(popup) {
    if (!popup) return;
    const wrap = popup.closest(".group-picker-wrap");
    if (!wrap) return;
    const btn = wrap.querySelector(".btn-add-group");
    if (!btn) return;
    const visibleItems = popup.querySelectorAll(".group-popup-item");
    const anyVisible = Array.from(visibleItems).some(it => it.style.display !== "none");
    btn.disabled = !anyVisible;
}

// ── Asset group management (detail row) ──

// Sync the collapsed row scope cell to match the detail row badges
function _syncCollapsedScope(assetId) {
    const row = document.querySelector(`tr.asset-row[data-asset-id="${assetId}"]`);
    if (!row) return;
    const scopeCell = row.querySelector("td:nth-child(4)");
    if (!scopeCell) return;
    const scopeEl = document.getElementById("scope-" + assetId);
    if (!scopeEl) return;
    // Gather current state from detail row
    const globalBadge = scopeEl.querySelector(".badge-ready");
    const personalBadge = scopeEl.querySelector(".badge-pending");
    const groupBadges = scopeEl.querySelectorAll(".badge[data-group-id]");
    scopeCell.innerHTML = "";
    if (globalBadge) {
        scopeCell.innerHTML = '<span class="badge badge-ready">Global</span>';
    } else if (groupBadges.length === 0) {
        scopeCell.innerHTML = '<span class="badge badge-pending">Personal</span>';
    } else {
        const MAX_SHOW = 2;
        groupBadges.forEach((b, i) => {
            if (i < MAX_SHOW) {
                const s = document.createElement("span");
                s.className = "badge badge-processing";
                s.style.marginRight = "0.15rem";
                s.textContent = b.textContent.replace("×", "").trim();
                scopeCell.appendChild(s);
            }
        });
        if (groupBadges.length > MAX_SHOW) {
            const extra = groupBadges.length - MAX_SHOW;
            const names = Array.from(groupBadges).slice(MAX_SHOW).map(b => b.textContent.replace("×", "").trim()).join(", ");
            const ov = document.createElement("span");
            ov.className = "badge badge-overflow has-tooltip";
            ov.innerHTML = `+${extra} more<span class="tooltip">${names}</span>`;
            scopeCell.appendChild(ov);
        }
    }
}

async function pickAssetGroup(assetId, groupId, groupName, btnEl) {
    closeAllGroupPopups();
    const resp = await apiCall("POST", `/api/assets/${assetId}/share?group_id=${groupId}`);
    if (resp && resp.ok) {
        const scopeEl = document.getElementById("scope-" + assetId);
        if (!scopeEl) { location.reload(); return; }
        // Remove any "Personal" badge
        const personalBadge = scopeEl.querySelector(".badge-pending");
        if (personalBadge) personalBadge.remove();
        // Add new badge before the + button wrapper
        const pickerWrap = scopeEl.querySelector(".group-picker-wrap");
        const badge = document.createElement("span");
        badge.className = "badge badge-processing";
        badge.dataset.groupId = groupId;
        badge.innerHTML = `${groupName} <button class="btn-x" onclick="event.stopPropagation(); unshareAsset('${assetId}', '${groupId}')" title="Remove from group">&times;</button>`;
        if (pickerWrap) scopeEl.insertBefore(badge, pickerWrap);
        else scopeEl.appendChild(badge);
        // Hide this option from the popup
        if (btnEl) btnEl.style.display = "none";
        const popup = document.getElementById("group-popup-" + assetId);
        _syncPlusButton(popup);
        _syncCollapsedScope(assetId);
    }
}

async function unshareAsset(assetId, groupId) {
    if (!await showConfirm("\u26a0\ufe0f This will remove the asset from everyone in this group. Continue?")) return;
    const resp = await apiCall("DELETE", `/api/assets/${assetId}/share?group_id=${groupId}`);
    if (resp && resp.ok) {
        const data = await resp.json();
        const scopeEl = document.getElementById("scope-" + assetId);
        if (!scopeEl) { location.reload(); return; }

        // If asset is no longer visible to us, remove its rows entirely
        if (data.still_visible === false) {
            const detailRow = document.getElementById("detail-" + assetId);
            const collapsedRow = detailRow ? detailRow.previousElementSibling : null;
            if (detailRow) detailRow.remove();
            if (collapsedRow) collapsedRow.remove();
            return;
        }

        // Capture group name before removing the badge
        const badge = scopeEl.querySelector(`.badge[data-group-id="${groupId}"]`);
        const groupName = badge ? badge.textContent.replace("\u00d7", "").trim() : "";
        if (badge) badge.remove();
        // Re-show or create option in popup
        const popup = document.getElementById("group-popup-" + assetId);
        if (popup) {
            let btn = popup.querySelector(`[data-group-id="${groupId}"]`);
            if (btn) {
                btn.style.display = "";
            } else if (groupName) {
                // Button wasn't rendered (group was assigned at page load) — create it
                btn = document.createElement("button");
                btn.type = "button";
                btn.className = "group-popup-item";
                btn.dataset.groupId = groupId;
                btn.textContent = groupName;
                btn.onclick = function(e) {
                    e.stopPropagation();
                    pickAssetGroup(assetId, groupId, groupName, btn);
                };
                popup.appendChild(btn);
            }
        }
        _syncPlusButton(popup);
        // If no badges left and not global, show Personal
        const remaining = scopeEl.querySelectorAll(".badge[data-group-id]");
        const globalBadge = scopeEl.querySelector(".badge-ready");
        if (remaining.length === 0 && !globalBadge) {
            const pickerWrap = scopeEl.querySelector(".group-picker-wrap");
            const personal = document.createElement("span");
            personal.className = "badge badge-pending";
            personal.textContent = "Personal";
            if (pickerWrap) scopeEl.insertBefore(personal, pickerWrap);
            else scopeEl.prepend(personal);
        }
        _syncCollapsedScope(assetId);
    }
}

async function toggleGlobal(assetId) {
    const resp = await apiCall("POST", `/api/assets/${assetId}/global`);
    if (resp && resp.ok) location.reload();
}

// ── Schedule actions ──
async function deleteSchedule(scheduleId) {
    if (!await showConfirm("Delete this schedule?")) return;
    const resp = await apiCall("DELETE", `/api/schedules/${scheduleId}`);
    if (resp && resp.ok) location.reload();
}

async function toggleSchedule(scheduleId, enabled) {
    const resp = await apiCall("PATCH", `/api/schedules/${scheduleId}`, { enabled });
    if (resp && resp.ok) location.reload();
}

async function createSchedule(form) {
    const data = new FormData(form);
    const startTime = data.get("start_time");
    const endTime = data.get("end_time");
    if (!startTime || !endTime) {
        showToast("Please select start and end times", true);
        return false;
    }
    if (startTime === endTime) {
        showToast("Start time and end time cannot be the same", true);
        return false;
    }
    const startDate = data.get("start_date");
    const endDate = data.get("end_date");
    if (startDate && endDate && endDate < startDate) {
        showToast("End date cannot be before start date", true);
        return false;
    }
    // Warn if end date is in the past
    if (endDate && endDate < new Date().toLocaleDateString('en-CA')) {
        const ok = await showConfirm("\u26a0\ufe0f The end date is in the past \u2014 this schedule will never play. Continue anyway?");
        if (!ok) return false;
    }
    const body = {
        name: data.get("name"),
        asset_id: data.get("asset_id"),
        start_time: startTime + ":00",
        end_time: endTime + ":00",
        priority: parseInt(data.get("priority") || "0"),
        enabled: true,
    };
    // Target
    const target = data.get("target_type");
    if (target === "device") body.device_id = data.get("target_id");
    else body.group_id = data.get("target_id");
    // Optional date range
    if (data.get("start_date")) body.start_date = data.get("start_date") + "T00:00:00Z";
    if (data.get("end_date")) body.end_date = data.get("end_date") + "T23:59:59Z";
    // Days of week
    const days = [];
    for (let i = 1; i <= 7; i++) {
        if (data.get(`day_${i}`)) days.push(i);
    }
    if (days.length > 0 && days.length < 7) body.days_of_week = days;

    // Explicit loop count (from round-to-loops)
    const loopCountVal = data.get("loop_count");
    if (loopCountVal) body.loop_count = parseInt(loopCountVal);

    const resp = await apiCall("POST", "/api/schedules", body);
    if (resp && resp.ok) {
        location.reload();
    } else if (resp) {
        const err = await resp.json();
        showToast(err.detail || JSON.stringify(err), true);
    }
    return false;
}

// ── User & Role Management ──

function showUserTab(tabId, btn) {
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.sub-tab').forEach(b => b.classList.remove('active'));
    document.getElementById(tabId).classList.add('active');
    btn.classList.add('active');
}

function toggleAllPermGroups(container, open) {
    container.querySelectorAll('details.perm-group').forEach(d => d.open = open);
}

function closeModal(id) {
    document.getElementById(id).style.display = 'none';
}

async function createUser(form) {
    const data = new FormData(form);
    const groupIds = data.getAll("group_ids");
    const body = {
        email: data.get("email"),
        display_name: data.get("display_name") || "",
        role_id: data.get("role_id"),
        group_ids: groupIds,
    };
    const resp = await apiCall("POST", "/api/users", body);
    if (resp && resp.ok) {
        showToast("User created — welcome email sent (if SMTP configured)");
        location.reload();
    } else if (resp) {
        const err = await resp.json();
        showToast(err.detail || JSON.stringify(err), true);
    }
}

let _editUserOriginal = {};

function _getEditUserState() {
    const groupIds = [];
    document.querySelectorAll('#edit-groups input[type="checkbox"]').forEach(cb => {
        if (cb.checked) groupIds.push(cb.value);
    });
    return {
        email: document.getElementById("edit-email").value,
        display_name: document.getElementById("edit-display-name").value,
        role_id: document.getElementById("edit-role").value,
        is_active: document.getElementById("edit-active").checked,
        password: document.getElementById("edit-password").value,
        group_ids: groupIds.sort().join(","),
    };
}

function _checkEditUserDirty() {
    const cur = _getEditUserState();
    const dirty = Object.keys(_editUserOriginal).some(k => cur[k] !== _editUserOriginal[k]);
    const btn = document.getElementById("edit-user-save-btn");
    if (btn) btn.disabled = !dirty;
}

function openEditUser(userId) {
    const u = usersData[userId];
    if (!u) return;
    document.getElementById("edit-user-id").value = userId;
    document.getElementById("edit-email").value = u.email;
    document.getElementById("edit-display-name").value = u.display_name || "";
    document.getElementById("edit-password").value = "";
    document.getElementById("edit-role").value = u.role_id;
    document.getElementById("edit-active").checked = u.is_active;
    document.querySelectorAll('#edit-groups input[type="checkbox"]').forEach(cb => {
        cb.checked = u.group_ids.includes(cb.value);
    });
    _editUserOriginal = _getEditUserState();
    const btn = document.getElementById("edit-user-save-btn");
    if (btn) btn.disabled = true;
    // Wire dirty-tracking on every open (safe to re-add — same function ref dedupes)
    document.querySelectorAll('#edit-user-modal input, #edit-user-modal select').forEach(el => {
        el.removeEventListener('input', _checkEditUserDirty);
        el.removeEventListener('change', _checkEditUserDirty);
        el.addEventListener('input', _checkEditUserDirty);
        el.addEventListener('change', _checkEditUserDirty);
    });
    document.getElementById("edit-user-modal").style.display = "";
}

async function updateUser(form) {
    try {
        const data = new FormData(form);
        const userId = data.get("user_id");
        const groupIds = data.getAll("group_ids");
        const body = {
            email: data.get("email"),
            display_name: data.get("display_name") || "",
            role_id: data.get("role_id"),
            is_active: data.get("is_active") === "on",
            group_ids: groupIds,
        };
        const pw = data.get("password");
        if (pw) body.password = pw;
        const resp = await apiCall("PATCH", `/api/users/${userId}`, body);
        if (resp && resp.ok) {
            showToast("User updated");
            location.reload();
        } else if (resp) {
            const err = await resp.json().catch(() => ({}));
            showToast(err.detail || `Error: ${resp.status}`, true);
        }
    } catch (e) {
        showToast("Failed to update user: " + e.message, true);
    }
}

async function deleteUser(userId, email) {
    if (!await showConfirm(`Delete user "${email}"? This cannot be undone.`)) return;
    const resp = await apiCall("DELETE", `/api/users/${userId}`);
    if (resp && resp.ok) {
        showToast("User deleted");
        location.reload();
    } else if (resp) {
        const err = await resp.json();
        showToast(err.detail || JSON.stringify(err), true);
    }
}

async function toggleUserActive(userId, active) {
    const resp = await apiCall("PATCH", `/api/users/${userId}`, { is_active: active });
    if (resp && resp.ok) {
        showToast(active ? "User enabled" : "User disabled");
        location.reload();
    } else if (resp) {
        const err = await resp.json();
        showToast(err.detail || JSON.stringify(err), true);
    }
}

async function createRole(form) {
    const data = new FormData(form);
    const perms = data.getAll("permissions");
    const body = {
        name: data.get("name"),
        description: data.get("description") || "",
        permissions: perms,
    };
    const resp = await apiCall("POST", "/api/roles", body);
    if (resp && resp.ok) {
        showToast("Role created");
        location.reload();
    } else if (resp) {
        const err = await resp.json();
        showToast(err.detail || JSON.stringify(err), true);
    }
}

function openEditRole(roleId) {
    const r = rolesData[roleId];
    if (!r) return;
    document.getElementById("edit-role-id").value = roleId;
    document.getElementById("edit-role-name").value = r.name;
    document.getElementById("edit-role-desc").value = r.description;
    // Set permission checkboxes
    document.querySelectorAll('#edit-role-permissions input[type="checkbox"]').forEach(cb => {
        cb.checked = r.permissions.includes(cb.value);
    });
    document.getElementById("edit-role-modal").style.display = "";
}

async function updateRole(form) {
    const data = new FormData(form);
    const roleId = data.get("role_id");
    const perms = data.getAll("permissions");
    const body = {
        name: data.get("name"),
        description: data.get("description") || "",
        permissions: perms,
    };
    const resp = await apiCall("PATCH", `/api/roles/${roleId}`, body);
    if (resp && resp.ok) {
        showToast("Role updated");
        location.reload();
    } else if (resp) {
        const err = await resp.json();
        showToast(err.detail || JSON.stringify(err), true);
    }
}

async function deleteRole(roleId, roleName) {
    if (!await showConfirm(`Delete role "${roleName}"? Users with this role will need to be reassigned.`)) return;
    const resp = await apiCall("DELETE", `/api/roles/${roleId}`);
    if (resp && resp.ok) {
        showToast("Role deleted");
        location.reload();
    } else if (resp) {
        const err = await resp.json();
        showToast(err.detail || JSON.stringify(err), true);
    }
}
