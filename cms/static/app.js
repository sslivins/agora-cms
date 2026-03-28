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

document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-storage-mb]").forEach(el => {
        const used = parseInt(el.dataset.usedMb || "0");
        const cap = parseInt(el.dataset.storageMb);
        el.textContent = used ? humanStorage(used) + " / " + humanStorage(cap) : humanStorage(cap);
    });
    document.querySelectorAll("[data-utc]").forEach(el => {
        const d = new Date(el.dataset.utc);
        if (isNaN(d)) return;
        el.textContent = d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
            + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    });
});

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
async function approveDevice(deviceId) {
    const resp = await apiCall("PATCH", `/api/devices/${deviceId}`, { status: "approved" });
    if (resp && resp.ok) location.reload();
}

async function deleteDevice(deviceId) {
    if (!await showConfirm("Delete this device?")) return;
    const resp = await apiCall("DELETE", `/api/devices/${deviceId}`);
    if (resp && resp.ok) location.reload();
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
async function deleteAsset(assetId, filename) {
    if (!await showConfirm("Delete \"" + (filename || "this asset") + "\"?")) return;
    const resp = await apiCall("DELETE", `/api/assets/${assetId}`);
    if (resp && resp.ok) location.reload();
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

async function createSchedule(form) {
    const data = new FormData(form);
    const body = {
        name: data.get("name"),
        asset_id: data.get("asset_id"),
        start_time: data.get("start_time") + ":00",
        end_time: data.get("end_time") + ":00",
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
