// Composed-editor AI chat drawer.
//
// Lives inside the composed-slide editor (gated on the Assistant
// feature flag). The user describes a slide; the assistant edits the
// *draft* layout via the composed_editor MCP tools and the canvas
// refreshes when a write lands.
//
// It talks to the editor only through window.composedEditorBridge
// (getAssetId / isDirty / save / refreshFromServer) and to the server
// through two endpoints:
//   POST /composed/{assetId}/assistant/thread   → get-or-create the
//        editor-scoped thread bound to this composed asset.
//   POST /api/chat/threads/{threadId}/stream     → run one turn (SSE).
//
// The SSE parser mirrors cms/static/assistant.js. No approval cards:
// composed_editor tools run inline (draft-only), so a stray
// approval_request is tolerated, not rendered as an interactive card.
(function () {
    "use strict";

    const root = document.getElementById("cw-ai");
    if (!root) return; // drawer not rendered (flag off / create mode)

    const bridge = window.composedEditorBridge;
    const launcher = document.getElementById("cw-ai-launcher");
    const panel = document.getElementById("cw-ai-panel");
    const closeBtn = document.getElementById("cw-ai-close");
    const log = document.getElementById("cw-ai-log");
    const form = document.getElementById("cw-ai-form");
    const input = document.getElementById("cw-ai-input");
    const sendBtn = document.getElementById("cw-ai-send");

    const state = {
        threadId: null,
        threadPromise: null, // de-dupe concurrent get-or-create
        open: false,
        sending: false,
    };

    function assetId() {
        if (bridge && typeof bridge.getAssetId === "function") {
            const id = bridge.getAssetId();
            if (id) return id;
        }
        return root.dataset.assetId || null;
    }

    // ── Drawer open/close ────────────────────────────────────────────
    function setOpen(open) {
        state.open = open;
        panel.hidden = !open;
        launcher.setAttribute("aria-expanded", open ? "true" : "false");
        launcher.style.display = open ? "none" : "";
        if (open) {
            ensureThread();
            input.focus();
        }
    }
    window.cwAiToggle = function () { setOpen(!state.open); };
    launcher.addEventListener("click", () => setOpen(true));
    closeBtn.addEventListener("click", () => setOpen(false));

    // ── Thread get-or-create ─────────────────────────────────────────
    function ensureThread() {
        if (state.threadId) return Promise.resolve(state.threadId);
        if (state.threadPromise) return state.threadPromise;
        const id = assetId();
        if (!id) return Promise.resolve(null);
        state.threadPromise = fetch(
            "/composed/" + encodeURIComponent(id) + "/assistant/thread",
            { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
        )
            .then(async (resp) => {
                if (!resp.ok) throw new Error("thread → " + resp.status);
                const j = await resp.json();
                state.threadId = j.thread_id;
                return state.threadId;
            })
            .catch((e) => {
                addMsg("error", "Couldn't start the assistant: " + e.message);
                state.threadPromise = null; // allow retry on next send
                return null;
            });
        return state.threadPromise;
    }

    // ── Log rendering ────────────────────────────────────────────────
    function addMsg(kind, text) {
        const div = document.createElement("div");
        div.className = "cw-ai-msg " + kind;
        div.textContent = text;
        log.appendChild(div);
        log.scrollTop = log.scrollHeight;
        return div;
    }

    // The model emits Markdown; render assistant replies as sanitized HTML
    // (bold, lists, code) when marked + DOMPurify are present. User/tool/
    // error bubbles stay plain text. Mirrors cms/static/assistant.js.
    const _hasMarkdown =
        typeof window.marked !== "undefined" && typeof window.DOMPurify !== "undefined";
    if (_hasMarkdown) {
        window.marked.setOptions({ gfm: true, breaks: true });
    }
    function renderAssistantMarkdown(bubble, text) {
        if (!_hasMarkdown) {
            bubble.textContent = text || "";
            return;
        }
        bubble.innerHTML = window.DOMPurify.sanitize(window.marked.parse(text || ""));
        bubble.classList.add("is-markdown");
    }

    // ── Create-mode mint ─────────────────────────────────────────────
    // On a brand-new slide there's no asset yet, so the chat can't bind to
    // one. Mint the draft via the editor bridge (which POSTs /composed/ and
    // PATCHes the seeded layout, no redirect), then proceed as in edit mode.
    async function mintDraft() {
        if (!bridge || typeof bridge.save !== "function") return false;
        let ok = false;
        try { ok = await bridge.save(); } catch (_) { ok = false; }
        const id = assetId();
        if (!ok || !id) return false;
        // Keep the URL + manual-save behavior consistent with a normal first
        // save, and lock the create-only config inputs (name/global/groups)
        // now that the asset exists — they're read once at mint time and
        // would otherwise silently no-op on later manual saves.
        try {
            history.replaceState(null, "", "/assets/" + encodeURIComponent(id) + "/composed");
        } catch (_) { /* non-fatal */ }
        ["composed-name", "composed-global"].forEach((cid) => {
            const el = document.getElementById(cid);
            if (el) el.disabled = true;
        });
        document.querySelectorAll("input[name='composed_group_ids']").forEach((el) => {
            el.disabled = true;
        });
        return true;
    }

    // ── Submit / stream ──────────────────────────────────────────────
    window.cwAiSubmit = function (e) {
        if (e) e.preventDefault();
        if (state.sending) return false;
        const content = (input.value || "").trim();
        if (!content) return false;
        sendMessage(content);
        return false;
    };

    async function sendMessage(content) {
        if (state.sending) return;
        // Lock the UI up-front so a rapid double-submit can't mint the draft
        // twice or kick off two concurrent streams.
        state.sending = true;
        sendBtn.disabled = true;
        input.disabled = true;
        input.value = "";
        addMsg("user", content);

        let sawSuccessfulWrite = false;
        try {
            // Create mode: mint the draft asset so the chat can bind to it.
            if (!assetId()) {
                const minted = await mintDraft();
                if (!minted) {
                    addMsg(
                        "error",
                        "Couldn't save the draft — check the message at the top of "
                        + "the page (a slide name is required), then send again."
                    );
                    return;
                }
            }

            // The assistant reads server-side draft state, so flush any
            // unsaved manual edits first — otherwise the post-turn refresh
            // (which clears the dirty flag) would silently drop them.
            if (bridge && bridge.isDirty && bridge.isDirty()) {
                const ok = await bridge.save();
                if (!ok) {
                    addMsg("error", "Couldn't save your latest changes — fix the error above and try again.");
                    return;
                }
            }

            const threadId = await ensureThread();
            if (!threadId) return; // ensureThread already surfaced the error

            let assistantBubble = null;
            let assistantText = "";
            const ensureBubble = () => {
                if (!assistantBubble) assistantBubble = addMsg("assistant", "");
                return assistantBubble;
            };

            const resp = await fetch(
                "/api/chat/threads/" + encodeURIComponent(threadId) + "/stream",
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ content: content }),
                }
            );
            if (!resp.ok) {
                const errText = await resp.text();
                throw new Error(resp.status + ": " + errText);
            }
            await consumeSseStream(resp.body, (evt) => {
                switch (evt.event) {
                    case "token":
                        assistantText += (evt.data && evt.data.text) || "";
                        renderAssistantMarkdown(ensureBubble(), assistantText);
                        log.scrollTop = log.scrollHeight;
                        break;
                    case "tool_call":
                        addMsg("tool", "→ " + ((evt.data && evt.data.name) || "tool") + "…");
                        break;
                    case "tool_result": {
                        const name = (evt.data && evt.data.name) || "tool";
                        addMsg("tool", "✓ " + name);
                        if (name === "set_composed_widgets" && !toolResultIsError(evt.data)) {
                            sawSuccessfulWrite = true;
                        }
                        break;
                    }
                    case "approval_request":
                        // composed_editor tools never need approval; if one
                        // arrives, note it rather than blocking the drawer.
                        addMsg("tool", "(skipped an approval-gated action)");
                        break;
                    case "done":
                        break;
                    case "error":
                        addMsg("error", "Error: " + ((evt.data && evt.data.message) || "unknown"));
                        break;
                }
            });
        } catch (e) {
            addMsg("error", "Request failed: " + e.message);
        } finally {
            if (sawSuccessfulWrite && bridge && bridge.refreshFromServer) {
                try { await bridge.refreshFromServer(); } catch (_) { /* non-fatal */ }
            }
            state.sending = false;
            sendBtn.disabled = false;
            input.disabled = false;
            input.focus();
        }
    }

    // A tool result's content is a JSON string; success == no "error" key.
    function toolResultIsError(data) {
        const raw = data && data.content;
        if (typeof raw !== "string") return false;
        try {
            const parsed = JSON.parse(raw);
            return parsed && typeof parsed === "object" && "error" in parsed;
        } catch (_) {
            return false;
        }
    }

    // ── SSE byte-stream parser (mirrors assistant.js) ────────────────
    async function consumeSseStream(stream, onEvent) {
        const reader = stream.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = findFrameBoundary(buffer)) !== -1) {
                const raw = buffer.slice(0, idx);
                buffer = buffer.slice(idx).replace(/^(\r?\n){2}/, "");
                const evt = parseFrame(raw);
                if (evt) onEvent(evt);
            }
        }
    }

    function findFrameBoundary(buf) {
        const a = buf.indexOf("\n\n");
        const b = buf.indexOf("\r\n\r\n");
        if (a === -1) return b;
        if (b === -1) return a;
        return Math.min(a, b);
    }

    function parseFrame(raw) {
        let event = "message";
        let data = "";
        raw.split(/\r?\n/).forEach((line) => {
            if (line.startsWith(":")) return; // comment / heartbeat
            if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:")) data += line.slice(5).trim();
        });
        if (!data) return null;
        try { return { event: event, data: JSON.parse(data) }; }
        catch (e) { return { event: event, data: { _raw: data } }; }
    }

    // Submit on Enter (Shift+Enter = newline).
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            window.cwAiSubmit(e);
        }
    });
})();
