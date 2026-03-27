/* Agora CMS — client-side JavaScript */

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
    if (!confirm("Delete this device?")) return;
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
    if (!confirm("Delete this group?")) return;
    const resp = await apiCall("DELETE", `/api/devices/groups/${groupId}`);
    if (resp && resp.ok) location.reload();
}

// ── Asset actions ──
async function deleteAsset(assetId) {
    if (!confirm("Delete this asset?")) return;
    const resp = await apiCall("DELETE", `/api/assets/${assetId}`);
    if (resp && resp.ok) location.reload();
}

async function uploadAsset(form) {
    const data = new FormData(form);
    const resp = await fetch("/api/assets/upload", { method: "POST", body: data });
    if (resp.status === 401) {
        window.location.href = "/login";
        return;
    }
    if (resp.ok) {
        location.reload();
    } else {
        const err = await resp.json();
        alert(err.detail || "Upload failed");
    }
    return false;
}

// ── Schedule actions ──
async function deleteSchedule(scheduleId) {
    if (!confirm("Delete this schedule?")) return;
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
        alert(err.detail || JSON.stringify(err));
    }
    return false;
}
