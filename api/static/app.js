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
    const fileInput = document.getElementById("upload-file");
    const uploadBtn = document.getElementById("upload-btn");
    fileInput.addEventListener("change", () => {
        uploadBtn.disabled = !fileInput.files.length;
    });
    uploadForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const fileInput = document.getElementById("upload-file");
        const statusEl = document.getElementById("upload-status");
        const progressEl = document.getElementById("upload-progress");
        if (!fileInput.files.length) return;

        const formData = new FormData();
        formData.append("file", fileInput.files[0]);
        uploadBtn.disabled = true;
        statusEl.textContent = "Uploading…";
        progressEl.value = 0;
        progressEl.style.display = "block";

        const xhr = new XMLHttpRequest();
        xhr.upload.addEventListener("progress", (evt) => {
            if (evt.lengthComputable) {
                const pct = Math.round((evt.loaded / evt.total) * 100);
                progressEl.value = pct;
                statusEl.textContent = "Uploading… " + pct + "%";
            }
        });
        xhr.addEventListener("load", () => {
            if (xhr.status === 401) {
                window.location.href = "/login";
                return;
            }
            if (xhr.status >= 200 && xhr.status < 300) {
                statusEl.textContent = "Upload complete.";
                progressEl.value = 100;
                setTimeout(() => location.reload(), 500);
            } else {
                try {
                    const data = JSON.parse(xhr.responseText);
                    statusEl.textContent = "Error: " + (data.detail || xhr.statusText);
                } catch {
                    statusEl.textContent = "Error: " + xhr.statusText;
                }
                uploadBtn.disabled = false;
            }
        });
        xhr.addEventListener("error", () => {
            statusEl.textContent = "Error: upload failed";
            progressEl.style.display = "none";
            uploadBtn.disabled = false;
        });
        xhr.open("POST", "/api/v1/assets/upload");
        xhr.send(formData);
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

/* ── Set Splash ── */
async function setSplash(name) {
    const res = await apiCall("POST", "/assets/" + encodeURIComponent(name) + "/set-splash");
    if (res && res.ok) {
        alert(name + " set as splash screen.");
    } else if (res) {
        const data = await res.json();
        alert("Error: " + (data.detail || res.statusText));
    }
}

/* ── Status polling ── */
function pollAndReload(expectedMode, maxAttempts = 10, interval = 1000) {
    let attempts = 0;
    const poll = setInterval(async () => {
        attempts++;
        try {
            const res = await apiCall("GET", "/status");
            if (res && res.ok) {
                const data = await res.json();
                if (!expectedMode || data.current.mode === expectedMode) {
                    clearInterval(poll);
                    location.reload();
                    return;
                }
            }
        } catch (e) { /* ignore */ }
        if (attempts >= maxAttempts) {
            clearInterval(poll);
            location.reload();
        }
    }, interval);
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
        pollAndReload("play");
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
        pollAndReload("splash");
    }
}

async function showSplash() {
    const statusEl = document.getElementById("playback-status");
    const res = await apiCall("POST", "/splash");
    if (res && res.ok) {
        statusEl.textContent = "Showing splash.";
        pollAndReload("splash");
    }
}
