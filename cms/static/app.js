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
// Second arg accepts:
//   - boolean: true = error, false/undefined = success (legacy)
//   - string:  'error' | 'success' | 'warning' | 'info'
// Error toasts stay on-screen longer and use a shake-in animation so users
// are less likely to miss them after a failed action (#248).
function showToast(message, variant) {
    let kind = "success";
    if (variant === true) kind = "error";
    else if (typeof variant === "string") {
        const v = variant.toLowerCase();
        if (v === "error" || v === "success" || v === "warning" || v === "info") {
            kind = v;
        }
    }
    const isError = kind === "error";
    const isWarning = kind === "warning";
    const el = document.createElement("div");
    el.className = "toast toast-" + kind;
    el.setAttribute("role", isError ? "alert" : "status");
    el.setAttribute("aria-live", isError ? "assertive" : "polite");

    const icon = document.createElement("span");
    icon.className = "toast-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = isError ? "✕" : isWarning ? "⚠" : kind === "info" ? "ℹ" : "✓";
    const body = document.createElement("span");
    body.className = "toast-body";
    body.textContent = message;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.onclick = () => el.remove();
    el.appendChild(icon);
    el.appendChild(body);
    el.appendChild(close);
    document.body.appendChild(el);

    // Error toasts linger longer so they're harder to miss.
    const lifetimeMs = isError ? 7000 : isWarning ? 5000 : 3000;
    setTimeout(() => el.remove(), lifetimeMs);
}

function extractErrorMsg(err, fallback) {
    if (!err) return fallback || "Unknown error";
    const d = err.detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) return d.map(e => e.msg || e.message || JSON.stringify(e)).join('; ');
    return fallback || JSON.stringify(err);
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
            + " " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true });
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

function toggleAuditRow(row) {
    const auditId = row.dataset.auditId;
    const detail = row.nextElementSibling;
    if (!detail || detail.dataset.detailFor !== auditId) return;
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

function editDeviceName(el) {
    const deviceId = el.dataset.deviceId;
    const fallback = el.dataset.fallback;
    const currentName = el.textContent.trim();

    const input = document.createElement('input');
    input.type = 'text';
    input.value = currentName === fallback ? '' : currentName;
    input.placeholder = fallback;
    input.className = 'form-control';
    input.style.cssText = 'font-size:0.85rem; padding:0.15rem 0.35rem; width:100%; max-width:220px;';

    const parent = el.parentElement;
    parent.replaceChild(input, el);
    input.focus();
    input.select();

    async function save() {
        const newName = input.value.trim();
        if (newName !== currentName && (newName || currentName !== fallback)) {
            const resp = await apiCall("PATCH", `/api/devices/${deviceId}`, { name: newName });
            if (resp && resp.ok) {
                el.textContent = newName || fallback;
                showToast("Device renamed");
            } else {
                el.textContent = currentName;
                showToast("Rename failed", true);
            }
        } else {
            el.textContent = currentName;
        }
        parent.replaceChild(el, input);
    }

    input.addEventListener('blur', save);
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
        if (e.key === 'Escape') { e.preventDefault(); el.textContent = currentName; parent.replaceChild(el, input); }
    });
    input.addEventListener('click', e => e.stopPropagation());
}

async function assignGroup(deviceId, groupId) {
    const resp = await apiCall("PATCH", `/api/devices/${deviceId}`, { group_id: groupId || null });
    if (resp && resp.ok) showToast("Group updated");
    else showToast("Group update failed", true);
}

async function setDefaultAsset(deviceId, assetId, selectEl) {
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

async function setDeviceField(deviceId, field, value) {
    const resp = await apiCall("PATCH", `/api/devices/${deviceId}`, { [field]: value || "" });
    if (resp && resp.ok) showToast(`${field.charAt(0).toUpperCase() + field.slice(1)} updated`);
    else showToast(`${field} update failed`, true);
}

async function setGroupDefaultAsset(groupId, assetId) {
    const resp = await apiCall("PATCH", `/api/devices/groups/${groupId}`, { default_asset_id: assetId || null });
    if (resp && resp.ok) showToast("Group default asset updated");
    else showToast("Update failed", true);
}

function editGroupField(el) {
    const groupId = el.dataset.groupId;
    const field = el.dataset.field;
    const placeholder = el.dataset.placeholder || '';
    const currentText = el.textContent.trim();
    const isPlaceholder = placeholder && currentText === placeholder;

    const input = document.createElement('input');
    input.type = 'text';
    input.value = isPlaceholder ? '' : currentText;
    input.placeholder = placeholder || field;
    input.className = 'form-control';
    input.style.cssText = 'font-size:0.85rem; padding:0.15rem 0.35rem; display:inline-block; width:auto; min-width:120px; max-width:300px;';

    const parent = el.parentElement;
    parent.replaceChild(input, el);
    input.focus();
    input.select();

    async function save() {
        const newVal = input.value.trim();
        if (newVal !== currentText && (newVal || !isPlaceholder)) {
            const body = {};
            body[field] = newVal || null;
            const resp = await apiCall("PATCH", `/api/devices/groups/${groupId}`, body);
            if (resp && resp.ok) {
                el.textContent = newVal || placeholder || '';
                showToast("Group updated");
            } else {
                el.textContent = currentText;
                showToast("Update failed", true);
            }
        } else {
            el.textContent = currentText;
        }
        parent.replaceChild(el, input);
    }

    input.addEventListener('blur', save);
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
        if (e.key === 'Escape') { e.preventDefault(); el.textContent = currentText; parent.replaceChild(el, input); }
    });
    input.addEventListener('click', e => e.stopPropagation());
}

async function adoptDevice(deviceId, deviceName) {
    // Show adoption modal with name + optional location + optional group + required profile
    const groups = window._adoptionGroups || [];
    const profiles = window._adoptionProfiles || [];
    const result = await showAdoptModal(deviceName || deviceId, groups, profiles);
    if (!result) return;
    const body = { name: result.name, profile_id: result.profile_id };
    if (result.location) body.location = result.location;
    if (result.group_id) body.group_id = result.group_id;
    const resp = await apiCall("POST", `/api/devices/${deviceId}/adopt`, body);
    if (resp && resp.ok) location.reload();
    else if (resp) {
        const err = await resp.json().catch(() => null);
        showToast(err?.detail || "Failed to adopt device", true);
    }
}

function showAdoptModal(defaultName, groups, profiles) {
    return new Promise((resolve) => {
        const overlay = document.createElement("div");
        overlay.className = "modal-overlay";
        const box = document.createElement("div");
        box.className = "modal-box";

        const title = document.createElement("h3");
        title.textContent = "Adopt Device";
        title.style.margin = "0 0 1rem 0";

        // Name field (required)
        const nameLabel = document.createElement("label");
        nameLabel.textContent = "Device Name";
        nameLabel.className = "modal-label";
        const nameInput = document.createElement("input");
        nameInput.type = "text";
        nameInput.className = "modal-input";
        nameInput.value = defaultName;
        nameInput.placeholder = "Enter device name";

        // Encoder Profile field (required)
        const profileLabel = document.createElement("label");
        profileLabel.textContent = "Encoder Profile";
        profileLabel.className = "modal-label";
        const profileSelect = document.createElement("select");
        profileSelect.className = "modal-input";
        const profilePlaceholder = document.createElement("option");
        profilePlaceholder.value = "";
        profilePlaceholder.textContent = "Select a profile…";
        profilePlaceholder.disabled = true;
        profilePlaceholder.selected = true;
        profileSelect.appendChild(profilePlaceholder);
        (profiles || []).forEach(p => {
            const opt = document.createElement("option");
            opt.value = p.id;
            opt.textContent = p.name;
            profileSelect.appendChild(opt);
        });

        // Location field (optional)
        const locLabel = document.createElement("label");
        locLabel.textContent = "Location (optional)";
        locLabel.className = "modal-label";
        const locInput = document.createElement("input");
        locInput.type = "text";
        locInput.className = "modal-input";
        locInput.placeholder = "e.g. Lobby, Conference Room A";

        // Group field (optional)
        const groupLabel = document.createElement("label");
        groupLabel.textContent = "Group (optional)";
        groupLabel.className = "modal-label";
        const groupSelect = document.createElement("select");
        groupSelect.className = "modal-input";
        const noneOpt = document.createElement("option");
        noneOpt.value = "";
        noneOpt.textContent = "None";
        groupSelect.appendChild(noneOpt);
        groups.forEach(g => {
            const opt = document.createElement("option");
            opt.value = g.id;
            opt.textContent = g.name;
            groupSelect.appendChild(opt);
        });

        // Actions
        const actions = document.createElement("div");
        actions.className = "modal-actions";
        const cancelBtn = document.createElement("button");
        cancelBtn.className = "btn btn-secondary";
        cancelBtn.textContent = "Cancel";
        const adoptBtn = document.createElement("button");
        adoptBtn.className = "btn btn-primary";
        adoptBtn.textContent = "Adopt";

        const validateForm = () => {
            adoptBtn.disabled = !nameInput.value.trim() || !profileSelect.value;
        };
        validateForm();

        nameInput.addEventListener("input", validateForm);
        profileSelect.addEventListener("change", validateForm);

        const getResult = () => ({
            name: nameInput.value.trim(),
            profile_id: profileSelect.value,
            location: locInput.value.trim() || null,
            group_id: groupSelect.value || null,
        });

        actions.appendChild(cancelBtn);
        actions.appendChild(adoptBtn);
        box.appendChild(title);
        box.appendChild(nameLabel);
        box.appendChild(nameInput);
        box.appendChild(profileLabel);
        box.appendChild(profileSelect);
        box.appendChild(locLabel);
        box.appendChild(locInput);
        box.appendChild(groupLabel);
        box.appendChild(groupSelect);
        box.appendChild(actions);
        overlay.appendChild(box);
        document.body.appendChild(overlay);
        nameInput.focus();
        nameInput.select();

        const close = (val) => { overlay.remove(); resolve(val); };
        cancelBtn.onclick = () => close(null);
        adoptBtn.onclick = () => {
            if (!nameInput.value.trim() || !profileSelect.value) return;
            close(getResult());
        };
        nameInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && nameInput.value.trim() && profileSelect.value) {
                close(getResult());
            }
        });
    });
}

async function deleteDevice(deviceId) {
    let msg = "Delete this device? It will need to be re-adopted to reconnect.";
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
    if (assetType === "video" || assetType === "saved_stream") {
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

function previewVariant(variantId, filename, assetType, profileName) {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    const box = document.createElement("div");
    box.className = "modal-box preview-box";
    const header = document.createElement("div");
    header.className = "preview-header";
    const title = document.createElement("span");
    title.textContent = profileName ? filename + " (" + profileName + ")" : filename || "Variant Preview";
    const closeBtn = document.createElement("button");
    closeBtn.className = "btn btn-secondary btn-sm";
    closeBtn.textContent = "Close";
    closeBtn.onclick = () => overlay.remove();
    header.appendChild(title);
    header.appendChild(closeBtn);
    box.appendChild(header);

    const url = `/api/assets/variants/${variantId}/preview`;
    if (assetType === "video" || assetType === "saved_stream") {
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

async function editAssetName(el) {
    const assetId = el.dataset.assetId;
    const currentName = el.textContent.trim();

    // Replace span with an input
    const input = document.createElement('input');
    input.type = 'text';
    input.value = currentName;
    input.className = 'form-control';
    input.style.cssText = 'font-size:0.85rem; padding:0.15rem 0.35rem; width:100%; max-width:300px;';

    const parent = el.parentElement;
    parent.replaceChild(input, el);
    input.focus();
    input.select();

    async function save() {
        const newName = input.value.trim();
        if (newName && newName !== currentName) {
            try {
                const resp = await fetch(`/api/assets/${assetId}`, {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({display_name: newName}),
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    showToast(extractErrorMsg(err) || 'Rename failed', true);
                    revert();
                    return;
                }
                el.textContent = newName;
                // Update tooltip too
                const tooltip = parent.querySelector('.tooltip');
                if (tooltip) tooltip.textContent = newName;
            } catch (e) {
                showToast('Rename failed: ' + e.message, true);
                revert();
                return;
            }
        }
        parent.replaceChild(el, input);
    }

    function revert() {
        parent.replaceChild(el, input);
    }

    input.addEventListener('blur', save);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
        if (e.key === 'Escape') { e.preventDefault(); revert(); }
    });
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

async function recaptureStream(assetId, displayName) {
    if (!await showConfirm(
        "Re-capture \"" + (displayName || "this stream") + "\"?\n\n" +
        "This will re-download the stream and redo all transcodes. " +
        "The existing file and all variants will be overwritten."
    )) return;
    const resp = await apiCall("POST", `/api/assets/${assetId}/recapture`);
    if (resp && resp.ok) {
        showToast("Re-capture started — variants will be re-transcoded.");
        location.reload();
    } else if (resp) {
        const err = await resp.json().catch(() => null);
        showToast(extractErrorMsg(err), true);
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

// ── Webpage asset creation ──

async function addWebpageAsset(form) {
    const urlInput = document.getElementById("webpage-url");
    const nameInput = document.getElementById("webpage-name");
    const statusEl = document.getElementById("webpage-status");
    const submitBtn = document.getElementById("webpage-submit");
    const url = urlInput.value.trim();
    if (!url) return;

    // Collect group IDs
    const badges = document.querySelectorAll("#webpage-groups-badges .badge[data-group-id]");
    const groupIds = Array.from(badges).map(b => b.dataset.groupId);

    submitBtn.disabled = true;
    statusEl.textContent = "Adding webpage...";
    statusEl.className = "form-status";

    try {
        const resp = await fetch("/api/assets/webpage", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                url: url,
                name: nameInput.value.trim(),
                group_ids: groupIds,
            }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        statusEl.textContent = "✓ Webpage added successfully";
        statusEl.className = "form-status text-success";
        urlInput.value = "";
        nameInput.value = "";
        setTimeout(() => location.reload(), 800);
    } catch (e) {
        statusEl.textContent = "✗ " + e.message;
        statusEl.className = "form-status text-danger";
        submitBtn.disabled = false;
    }
}

function pickWebpageGroup(gid, name) {
    const container = document.getElementById("webpage-groups-badges");
    if (!container || container.querySelector(`.badge[data-group-id="${gid}"]`)) return;
    const plusBtn = container.querySelector(".group-picker-wrap");
    const badge = document.createElement("span");
    badge.className = "badge badge-processing";
    badge.dataset.groupId = gid;
    badge.innerHTML = `${name} <button class="btn-x" type="button" onclick="removeWebpageGroup(this.parentElement)">&times;</button>`;
    container.insertBefore(badge, plusBtn);
    const popup = document.getElementById("webpage-group-popup");
    if (popup) {
        const btn = popup.querySelector(`[data-group-id="${gid}"]`);
        if (btn) btn.style.display = "none";
        _syncPlusButton(popup);
    }
    closeAllGroupPopups();
}

function removeWebpageGroup(badge) {
    const gid = badge.dataset.groupId;
    badge.remove();
    const popup = document.getElementById("webpage-group-popup");
    if (popup && gid) {
        const btn = popup.querySelector(`[data-group-id="${gid}"]`);
        if (btn) btn.style.display = "";
        _syncPlusButton(popup);
    }
}

// ── Stream asset functions ──

// State from the most recent probe
let _lastProbe = null;

function _formatDuration(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    return `${m}:${String(s).padStart(2,'0')}`;
}

function _formatBitrate(kbps) {
    if (kbps >= 1000) return (kbps / 1000).toFixed(1) + ' Mbps';
    return kbps + ' kbps';
}

async function probeStreamUrl(url) {
    const card = document.getElementById('stream-info-card');
    const content = document.getElementById('stream-info-content');
    _lastProbe = null;

    if (!url || !url.trim()) {
        card.style.display = 'none';
        onSaveLocallyChanged();
        return;
    }

    content.innerHTML = '<span style="color:var(--text-muted)">⏳ Inspecting stream…</span>';
    card.style.display = 'block';

    try {
        const resp = await fetch('/api/streams/probe?url=' + encodeURIComponent(url.trim()));
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            content.innerHTML = '<span style="color:var(--danger)">⚠ ' + (err.detail || 'Failed to probe stream') + '</span>';
            return;
        }
        const info = await resp.json();
        _lastProbe = info;

        let html = '<div style="display:flex; gap:1.5rem; flex-wrap:wrap; align-items:flex-start;">';

        // Stream type indicator
        const isLive = info.is_live;
        const typeLabel = isLive ? '🔴 Live Stream' : (isLive === false ? '📼 VOD' : '❓ Unknown');
        html += '<div><strong>' + typeLabel + '</strong></div>';

        // Info columns
        html += '<div style="display:grid; grid-template-columns:auto auto; gap:0.15rem 0.75rem; font-size:0.82rem;">';

        if (info.type)
            html += '<span style="color:var(--text-muted)">Format:</span><span>' + info.type.toUpperCase() + '</span>';
        if (info.resolution)
            html += '<span style="color:var(--text-muted)">Resolution:</span><span>' + info.resolution + '</span>';
        if (info.codecs)
            html += '<span style="color:var(--text-muted)">Codec:</span><span>' + info.codecs + '</span>';
        else if (info.video_codec)
            html += '<span style="color:var(--text-muted)">Codec:</span><span>' + info.video_codec + (info.audio_codec ? ' + ' + info.audio_codec : '') + '</span>';
        if (info.frame_rate)
            html += '<span style="color:var(--text-muted)">Frame rate:</span><span>' + info.frame_rate + ' fps</span>';
        if (info.duration_seconds)
            html += '<span style="color:var(--text-muted)">Duration:</span><span>' + _formatDuration(info.duration_seconds) + '</span>';

        html += '</div>';

        // Variants list
        if (info.variants && info.variants.length > 1) {
            html += '<div style="font-size:0.82rem;">';
            html += '<span style="color:var(--text-muted)">Variants (' + info.variants.length + '):</span><br>';
            info.variants.sort((a, b) => (b.bandwidth_kbps || 0) - (a.bandwidth_kbps || 0));
            for (const v of info.variants) {
                const parts = [];
                if (v.resolution) parts.push(v.resolution);
                if (v.bandwidth_kbps) parts.push(_formatBitrate(v.bandwidth_kbps));
                html += '<span style="margin-left:0.5rem;">' + parts.join(' · ') + '</span><br>';
            }
            html += '</div>';
        }

        html += '</div>';
        content.innerHTML = html;

    } catch (e) {
        content.innerHTML = '<span style="color:var(--danger)">⚠ ' + e.message + '</span>';
    }

    onSaveLocallyChanged();
}

function onSaveLocallyChanged() {
    const saveLocally = document.getElementById('stream-save-locally').checked;
    const durGroup = document.getElementById('stream-duration-group');
    const durSelect = document.getElementById('stream-capture-duration');
    const customInput = document.getElementById('stream-capture-custom');

    // Show duration field when saving a live stream
    const isLive = _lastProbe && _lastProbe.is_live;
    if (saveLocally && isLive) {
        durGroup.style.display = 'block';
        durSelect.required = true;
    } else {
        durGroup.style.display = 'none';
        durSelect.required = false;
        durSelect.value = '';
        customInput.style.display = 'none';
        customInput.required = false;
    }
    // Toggling `required` programmatically doesn't emit input/change —
    // re-run the form's gate updater so the submit button reflects the
    // new required set. See issue #348.
    document.getElementById('stream-form')?.__gateUpdate?.();
}

// Handle custom duration dropdown
document.addEventListener('DOMContentLoaded', () => {
    const sel = document.getElementById('stream-capture-duration');
    const custom = document.getElementById('stream-capture-custom');
    if (sel) {
        sel.addEventListener('change', () => {
            const isCustom = sel.value === 'custom';
            custom.style.display = isCustom ? 'inline-block' : 'none';
            if (!isCustom) custom.value = '';
            // When "Custom…" is picked, the inline number input becomes
            // the required field instead of the select — keep the submit
            // gate honest.
            custom.required = isCustom;
            document.getElementById('stream-form')?.__gateUpdate?.();
        });
    }
});

function _getCaptureDuration() {
    const sel = document.getElementById('stream-capture-duration');
    if (!sel || !sel.value) return null;
    if (sel.value === 'custom') {
        const v = parseInt(document.getElementById('stream-capture-custom').value, 10);
        return isNaN(v) ? null : v;
    }
    return parseInt(sel.value, 10);
}

async function addStreamAsset(form) {
    const urlInput = document.getElementById("stream-url");
    const nameInput = document.getElementById("stream-name");
    const saveLocallyEl = document.getElementById("stream-save-locally");
    const statusEl = document.getElementById("stream-status");
    const submitBtn = document.getElementById("stream-submit");
    const url = urlInput.value.trim();
    if (!url) return;

    const badges = document.querySelectorAll("#stream-groups-badges .badge[data-group-id]");
    const groupIds = Array.from(badges).map(b => b.dataset.groupId);

    // Build payload
    const payload = {
        url: url,
        name: nameInput.value.trim(),
        save_locally: saveLocallyEl.checked,
        group_ids: groupIds,
    };

    // Add capture_duration if saving a live stream
    if (saveLocallyEl.checked && _lastProbe && _lastProbe.is_live) {
        const dur = _getCaptureDuration();
        if (!dur) {
            statusEl.textContent = "✗ Please select a capture duration for this live stream";
            statusEl.className = "form-status text-danger";
            return;
        }
        payload.capture_duration = dur;
    }

    submitBtn.disabled = true;
    statusEl.textContent = "Adding stream...";
    statusEl.className = "form-status";

    try {
        const resp = await fetch("/api/assets/stream", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(payload),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(extractErrorMsg(err));
        }
        statusEl.textContent = "✓ Stream added successfully";
        statusEl.className = "form-status text-success";
        urlInput.value = "";
        nameInput.value = "";
        document.getElementById('stream-info-card').style.display = 'none';
        _lastProbe = null;
        setTimeout(() => location.reload(), 800);
    } catch (e) {
        statusEl.textContent = "✗ " + e.message;
        statusEl.className = "form-status text-danger";
        submitBtn.disabled = false;
    }
}

function pickStreamGroup(gid, name) {
    const container = document.getElementById("stream-groups-badges");
    if (!container || container.querySelector(`.badge[data-group-id="${gid}"]`)) return;
    const plusBtn = container.querySelector(".group-picker-wrap");
    const badge = document.createElement("span");
    badge.className = "badge badge-processing";
    badge.dataset.groupId = gid;
    badge.innerHTML = `${name} <button class="btn-x" type="button" onclick="removeStreamGroup(this.parentElement)">&times;</button>`;
    container.insertBefore(badge, plusBtn);
    const popup = document.getElementById("stream-group-popup");
    if (popup) {
        const btn = popup.querySelector(`[data-group-id="${gid}"]`);
        if (btn) btn.style.display = "none";
        _syncPlusButton(popup);
    }
    closeAllGroupPopups();
}

function removeStreamGroup(badge) {
    const gid = badge.dataset.groupId;
    badge.remove();
    const popup = document.getElementById("stream-group-popup");
    if (popup && gid) {
        const btn = popup.querySelector(`[data-group-id="${gid}"]`);
        if (btn) btn.style.display = "";
        _syncPlusButton(popup);
    }
}

// ── Group popup (shared for upload + detail row) ──
// Native popover API — browser handles open/close/light-dismiss/Escape.
// popover="auto" auto-closes any sibling popover when a new one opens.
function openGroupPopup(popupId) {
    const popup = document.getElementById(popupId);
    if (!popup) return;
    if (popup.matches(":popover-open")) {
        popup.hidePopover();
    } else {
        popup.showPopover();
    }
}

function closeAllGroupPopups() {
    document.querySelectorAll(".group-popup:popover-open").forEach(p => p.hidePopover());
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
            const collapsedRow = document.querySelector(`tr.asset-row[data-asset-id="${assetId}"]`);
            const detailRow = document.querySelector(`tr.asset-detail[data-detail-for="${assetId}"]`);
            if (collapsedRow) collapsedRow.remove();
            if (detailRow) detailRow.remove();
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
    // Implemented per-page in assets.html (IIFE scope has __userGroups/__isAdmin
    // and _rebuildDetailScope). This stub keeps older inline onclick handlers
    // working if app.js loads first; assets.html overwrites window.toggleGlobal.
    if (window._toggleGlobalImpl) return window._toggleGlobalImpl(assetId);
    location.reload();
}

// ── Schedule actions ──
async function deleteSchedule(scheduleId) {
    const playing = (window._playingScheduleIds || []).includes(scheduleId);
    const msg = playing
        ? "This schedule is currently playing. Deleting it will immediately stop playback on all devices in the group."
        : "Delete this schedule?";
    if (!await showConfirm(msg)) return;
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
    // FormData excludes disabled inputs — read end_time from the DOM directly
    const endTime = data.get("end_time") || form.querySelector('[name="end_time"]').value;
    const loopCountVal = data.get("loop_count");
    const hasLoopCount = loopCountVal && parseInt(loopCountVal) > 0;
    if (!startTime) {
        showToast("Please select a start time", true);
        return false;
    }
    if (!hasLoopCount && !endTime) {
        showToast("Please select an end time or set a loop count", true);
        return false;
    }
    if (!hasLoopCount && startTime === endTime) {
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
        start_time: startTime.length <= 5 ? startTime + ":00" : startTime,
        priority: parseInt(data.get("priority") || "0"),
        enabled: true,
    };
    // end_time: include if present (auto-computed or manual)
    if (endTime) body.end_time = endTime.length <= 5 ? endTime + ":00" : endTime;
    // Explicit loop count
    if (hasLoopCount) body.loop_count = parseInt(loopCountVal);
    // Target — always group
    body.group_id = data.get("group_id");
    // Optional date range
    if (data.get("start_date")) body.start_date = data.get("start_date") + "T00:00:00Z";
    if (data.get("end_date")) body.end_date = data.get("end_date") + "T23:59:59Z";
    // Days of week
    const days = [];
    for (let i = 1; i <= 7; i++) {
        if (data.get(`day_${i}`)) days.push(i);
    }
    if (days.length > 0 && days.length < 7) body.days_of_week = days;

    const resp = await apiCall("POST", "/api/schedules", body);
    if (resp && resp.ok) {
        location.reload();
    } else if (resp) {
        const err = await resp.json();
        showToast(extractErrorMsg(err), true);
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
        showToast(extractErrorMsg(err), true);
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
        showToast(extractErrorMsg(err), true);
    }
}

async function toggleUserActive(userId, active) {
    const resp = await apiCall("PATCH", `/api/users/${userId}`, { is_active: active });
    if (resp && resp.ok) {
        showToast(active ? "User enabled" : "User disabled");
        location.reload();
    } else if (resp) {
        const err = await resp.json();
        showToast(extractErrorMsg(err), true);
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
        showToast(extractErrorMsg(err), true);
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
        showToast(extractErrorMsg(err), true);
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
        showToast(extractErrorMsg(err), true);
    }
}


// ── Required-input gating ──────────────────────────────────────────────
// Disables primary action buttons until required inputs have values.
// See issues #315, #348 (autofill).

// Registry of all active update functions so we can re-run every gate when
// browser autofill (Chromium) silently populates fields without firing
// input/change events.
const _gateUpdaters = new Set();

/**
 * Check whether a form control has a non-empty value.
 * Checkboxes/radios: must be checked. Everything else: trimmed value non-empty.
 */
function _hasValue(el) {
    if (!el) return false;
    if (el.type === "checkbox" || el.type === "radio") return el.checked;
    // A disabled field with a filled value still counts as "has value" —
    // forms may read disabled inputs via JS (e.g. an auto-computed
    // end_time that's disabled when loop_count is set, but whose value
    // is explicitly submitted). Only empty values should block submit.
    return (el.value || "").trim() !== "";
}

function _rerunAllGates() {
    _gateUpdaters.forEach(fn => { try { fn(); } catch {} });
}

/**
 * Disable `button` until every element in `inputs` has a value.
 * Re-evaluates on input/change events. Safe to call multiple times — idempotent.
 *
 * @param {HTMLElement|string} button - button element or CSS selector
 * @param {Array<HTMLElement|string>} inputs - input elements or CSS selectors
 */
function gateButtonOnInputs(button, inputs) {
    const btn = typeof button === "string" ? document.querySelector(button) : button;
    if (!btn) return;
    const els = (inputs || [])
        .map(i => typeof i === "string" ? document.querySelector(i) : i)
        .filter(Boolean);
    if (!els.length) return;

    const update = () => {
        btn.disabled = !els.every(_hasValue);
    };
    els.forEach(el => {
        if (el.__gateBound) return;
        el.__gateBound = true;
        el.addEventListener("input", update);
        el.addEventListener("change", update);
        // Autofill detection (Chromium/Edge): the browser's :-webkit-autofill
        // pseudo-class triggers our dummy CSS animation, which fires
        // animationstart even when no input/change event is dispatched.
        el.addEventListener("animationstart", (e) => {
            if (e.animationName === "onAutoFillStart" ||
                e.animationName === "onAutoFillCancel") {
                update();
            }
        });
        // Re-check whenever the user interacts with the field — catches
        // autofill that lands on focus in some Chromium paths.
        el.addEventListener("focus", update);
        el.addEventListener("blur", update);
    });
    _gateUpdaters.add(update);
    update();
    return update;
}

/**
 * Auto-bind: for every <form data-gate-required>, disable its submit button
 * until all [required] fields inside are non-empty. Uses form-level event
 * delegation so fields that become required dynamically (e.g. a capture
 * duration select revealed by a toggle) are picked up automatically.
 */
function bindFormsRequiredGating(root) {
    (root || document).querySelectorAll("form[data-gate-required]").forEach(form => {
        if (form.__gateBound) return;
        form.__gateBound = true;
        const btn = form.querySelector('button[type="submit"], button.btn-primary');
        if (!btn) return;

        const update = () => {
            const required = Array.from(form.querySelectorAll("[required]"));
            btn.disabled = required.length > 0 && !required.every(_hasValue);
        };
        form.__gateUpdate = update;
        _gateUpdaters.add(update);

        // Event delegation at the form level catches current and future fields.
        form.addEventListener("input", update);
        form.addEventListener("change", update);
        // animationstart doesn't bubble in some engines — use capture phase.
        form.addEventListener("animationstart", (e) => {
            if (e.animationName === "onAutoFillStart" ||
                e.animationName === "onAutoFillCancel") {
                update();
            }
        }, true);

        update();
    });
}

document.addEventListener("DOMContentLoaded", () => bindFormsRequiredGating());

// Autofill typically completes between DOMContentLoaded and window.load —
// re-evaluate every gate once everything is settled.
window.addEventListener("load", () => {
    // Double rAF to ensure we run after the browser has applied autofill.
    requestAnimationFrame(() => requestAnimationFrame(_rerunAllGates));
});

// pageshow fires on back/forward cache restores, where autofill state
// may already be present without any events having fired.
window.addEventListener("pageshow", _rerunAllGates);


// ── Generic top-layer popover positioning ───────────────────────────────
// Native [popover] elements render in the top-layer with UA defaults
// (margin:auto, inset:0) that center them on the viewport. We override
// to position:fixed; inset:auto and set top/left here, since CSS
// anchor-positioning still has uneven cross-browser support (early 2026).
//
// opts.placement: "below" (default) or "above"
// opts.align:     "right" (default — right-edge to right-edge of anchor)
//                 or "left" (left-edge to left-edge of anchor)
function positionPopover(popover, opts = {}) {
    const btn = document.querySelector(`[popovertarget="${popover.id}"]`);
    if (!btn) return;
    const placement = opts.placement || "below";
    const align = opts.align || "right";
    const b = btn.getBoundingClientRect();
    // Clear inline coords so the popover's natural size is measured.
    popover.style.top = "";
    popover.style.left = "";
    const m = popover.getBoundingClientRect();
    const gap = 4;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    // Vertical: honor placement, but flip if it would overflow the viewport.
    let top;
    if (placement === "above") {
        top = b.top - gap - m.height;
        if (top < 8 && b.bottom + gap + m.height <= vh - 8) {
            top = b.bottom + gap;
        }
    } else {
        top = b.bottom + gap;
        if (top + m.height > vh - 8 && b.top - gap - m.height >= 8) {
            top = b.top - gap - m.height;
        }
    }

    // Horizontal alignment, clamped to viewport.
    let left = align === "left" ? b.left : b.right - m.width;
    if (left < 8) left = 8;
    if (left + m.width > vw - 8) left = vw - 8 - m.width;

    popover.style.top = `${top}px`;
    popover.style.left = `${left}px`;
}

// ── Kebab (⋮) action menu — #249 ──────────────────────────────────────
// Uses the native HTML popover API — open/close/light-dismiss/Escape are
// all handled by the browser. We only need to position the popover below
// (or above, if it would overflow) its invoker button.

function positionKebab(menu) { positionPopover(menu, { placement: "below", align: "right" }); }
function positionGroupPopup(p) { positionPopover(p, { placement: "above", align: "left" }); }

document.addEventListener("toggle", (e) => {
    const el = e.target;
    if (!(el instanceof HTMLElement)) return;
    const isKebab = el.classList.contains("kebab-menu");
    const isGroupPopup = el.classList.contains("group-popup");
    if (!isKebab && !isGroupPopup) return;
    const btn = document.querySelector(`[popovertarget="${el.id}"]`);
    if (e.newState === "open") {
        if (btn) btn.setAttribute("aria-expanded", "true");
        if (isKebab) positionKebab(el);
        else positionGroupPopup(el);
    } else if (btn) {
        btn.setAttribute("aria-expanded", "false");
    }
}, true);

// Stamp recent kebab clicks so live-refresh pollers can avoid re-rendering
// the actions cell during the click → popover-open window (which would
// detach the click target before the browser opens the popover).
document.addEventListener("mousedown", (e) => {
    const t = e.target;
    if (t instanceof Element && t.closest(".btn-kebab")) {
        window._kebabClickAt = Date.now();
    }
}, true);

// Re-position any open popover on viewport changes so it doesn't drift.
function _repositionAllOpenPopovers() {
    document.querySelectorAll(".kebab-menu:popover-open").forEach(positionKebab);
    document.querySelectorAll(".group-popup:popover-open").forEach(positionGroupPopup);
}
window.addEventListener("scroll", _repositionAllOpenPopovers, true);
window.addEventListener("resize", _repositionAllOpenPopovers);
