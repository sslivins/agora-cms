/* Agora CMS — client-side JavaScript */

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
            + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    });
    // Legacy storage formatters (other pages)
    document.querySelectorAll("[data-storage-mb]:not([data-storage-pct]):not([data-storage-detail])").forEach(el => {
        const used = parseInt(el.dataset.usedMb || "0");
        const cap = parseInt(el.dataset.storageMb);
        el.textContent = used ? humanStorage(used) + " / " + humanStorage(cap) : humanStorage(cap);
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

async function setDefaultAsset(deviceId, assetId) {
    const resp = await apiCall("PATCH", `/api/devices/${deviceId}`, { default_asset_id: assetId || null });
    if (resp && resp.ok) showToast("Default asset updated");
    else showToast("Update failed", true);
}

async function setGroupDefaultAsset(groupId, assetId) {
    const resp = await apiCall("PATCH", `/api/devices/groups/${groupId}`, { default_asset_id: assetId || null });
    if (resp && resp.ok) showToast("Group default asset updated");
    else showToast("Update failed", true);
}

async function approveDevice(deviceId) {
    const resp = await apiCall("PATCH", `/api/devices/${deviceId}`, { status: "approved" });
    if (resp && resp.ok) location.reload();
}

async function deleteDevice(deviceId) {
    if (!await showConfirm("Delete this device?")) return;
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
    if (!await showConfirm("Reboot device \"" + deviceName + "\"?")) return;
    const resp = await apiCall("POST", `/api/devices/${deviceId}/reboot`);
    if (resp && resp.ok) showToast("Reboot command sent to " + deviceName);
    else if (resp) {
        const err = await resp.json().catch(() => null);
        showToast(err?.detail || "Failed to reboot device", true);
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
        xhr.open("POST", "/api/assets/upload");
        xhr.send(data);
    });
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

function to24h(hour, minute, period) {
    let h = parseInt(hour);
    const m = parseInt(minute);
    if (period === "AM" && h === 12) h = 0;
    else if (period === "PM" && h !== 12) h += 12;
    return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0") + ":00";
}

async function createSchedule(form) {
    const data = new FormData(form);
    const body = {
        name: data.get("name"),
        asset_id: data.get("asset_id"),
        start_time: to24h(data.get("start_hour"), data.get("start_minute"), data.get("start_period")),
        end_time: to24h(data.get("end_hour"), data.get("end_minute"), data.get("end_period")),
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

    const resp = await apiCall("POST", "/api/schedules", body);
    if (resp && resp.ok) {
        location.reload();
    } else if (resp) {
        const err = await resp.json();
        showToast(err.detail || JSON.stringify(err), true);
    }
    return false;
}
