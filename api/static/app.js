/* Agora Web UI — vanilla JS for API interactions */

async function apiCall(method, path, body) {
    const opts = { method, headers: {} };
    if (body) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
    }
    const res = await fetch("/api/v1" + path, opts);
    if (res.status === 401) {
        window.location.href = "/login";
        return null;
    }
    return res;
}

/* ── Asset Upload ── */
const uploadForm = document.getElementById("upload-form");
if (uploadForm) {
    uploadForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const fileInput = document.getElementById("upload-file");
        const statusEl = document.getElementById("upload-status");
        if (!fileInput.files.length) return;

        const formData = new FormData();
        formData.append("file", fileInput.files[0]);
        statusEl.textContent = "Uploading…";

        try {
            const res = await fetch("/api/v1/assets/upload", {
                method: "POST",
                body: formData,
            });
            if (res.status === 401) {
                window.location.href = "/login";
                return;
            }
            if (res.ok) {
                statusEl.textContent = "Upload complete.";
                setTimeout(() => location.reload(), 500);
            } else {
                const data = await res.json();
                statusEl.textContent = "Error: " + (data.detail || res.statusText);
            }
        } catch (err) {
            statusEl.textContent = "Error: " + err.message;
        }
    });
}

/* ── Asset Delete ── */
async function deleteAsset(name) {
    if (!confirm("Delete " + name + "?")) return;
    const res = await apiCall("DELETE", "/assets/" + encodeURIComponent(name));
    if (res && res.ok) {
        location.reload();
    }
}

/* ── Playback Controls ── */
async function playAsset(e) {
    e.preventDefault();
    const asset = document.getElementById("asset-select").value;
    const loop = document.getElementById("loop-check").checked;
    const statusEl = document.getElementById("playback-status");
    if (!asset) return false;

    const res = await apiCall("POST", "/play", { asset: asset, loop: loop });
    if (res && res.ok) {
        statusEl.textContent = "Playing: " + asset;
        setTimeout(() => location.reload(), 1000);
    } else if (res) {
        const data = await res.json();
        statusEl.textContent = "Error: " + (data.detail || res.statusText);
    }
    return false;
}

async function stopPlayback() {
    const statusEl = document.getElementById("playback-status");
    const res = await apiCall("POST", "/stop");
    if (res && res.ok) {
        statusEl.textContent = "Stopped.";
        setTimeout(() => location.reload(), 1000);
    }
}

async function showSplash() {
    const statusEl = document.getElementById("playback-status");
    const res = await apiCall("POST", "/splash");
    if (res && res.ok) {
        statusEl.textContent = "Showing splash.";
        setTimeout(() => location.reload(), 1000);
    }
}
