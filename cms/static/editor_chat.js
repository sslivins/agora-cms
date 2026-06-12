// Shared in-editor AI chat drawer.
//
// Powers the embedded assistant drawer on BOTH the composed-slide
// editor and the slideshow builder. The user describes a change; the
// assistant edits the asset via its editor-scoped MCP tools and the
// editor refreshes when a write lands.
//
// The page configures it via window.cwAiConfig (all fields optional;
// the defaults reproduce the original composed-editor behaviour so
// composed_editor.html needs no config block):
//
//   window.cwAiConfig = {
//     bridge,                       // {getAssetId, isDirty, save, refreshFromServer}
//     threadPath: (assetId) => "…", // POST get-or-create editor thread
//     writeTools: ["set_…"],        // tool names whose success triggers a refresh
//     mint,                         // async () => bool, or null to disable
//   };
//
// It talks to the editor only through the bridge and to the server
// through two endpoints:
//   POST <threadPath(assetId)>                    → get-or-create thread
//   POST /api/chat/threads/{threadId}/stream       → run one turn (SSE).
//
// The SSE parser mirrors cms/static/assistant.js. No approval cards:
// editor tools run inline, so a stray approval_request is tolerated,
// not rendered as an interactive card.
(function () {
    "use strict";

    const root = document.getElementById("cw-ai");
    if (!root) return; // drawer not rendered (flag off / create mode)

    const cfg = window.cwAiConfig || {};
    const bridge = cfg.bridge || window.composedEditorBridge;
    const threadPath =
        typeof cfg.threadPath === "function"
            ? cfg.threadPath
            : (id) => "/composed/" + encodeURIComponent(id) + "/assistant/thread";
    const writeTools = Array.isArray(cfg.writeTools)
        ? cfg.writeTools
        : ["set_composed_widgets"];

    const launcher = document.getElementById("cw-ai-launcher");
    const panel = document.getElementById("cw-ai-panel");
    const closeBtn = document.getElementById("cw-ai-close");
    const log = document.getElementById("cw-ai-log");
    const form = document.getElementById("cw-ai-form");
    const input = document.getElementById("cw-ai-input");
    const sendBtn = document.getElementById("cw-ai-send");

    // Shared per-user usage strip (cms/static/assistant_usage.js). Mirrors
    // the main Assistant page: refreshed when the drawer opens and after
    // every completed turn. Hidden if the component or host is missing.
    const usageStrip =
        (window.AssistantUsage && document.getElementById("cw-ai-usage"))
            ? window.AssistantUsage.create(document.getElementById("cw-ai-usage"))
            : null;
    function refreshUsage() {
        if (usageStrip) usageStrip.refresh();
    }

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
            refreshUsage();
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
            threadPath(id),
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
    // On a brand-new composed slide there's no asset yet, so the chat
    // can't bind to one. Mint the draft via the editor bridge (which
    // POSTs /composed/ and PATCHes the seeded layout, no redirect), then
    // proceed as in edit mode. Pages that don't support create-mode chat
    // (e.g. the slideshow builder) pass mint: null to disable this.

    // A friendly, sortable default name for slides the user starts via the
    // assistant without typing one. Built from local Date parts (not
    // toLocaleString) so it's locale-stable and lexically sortable.
    function defaultSlideName() {
        const d = new Date();
        const p = (n) => String(n).padStart(2, "0");
        return "Untitled slide " + d.getFullYear() + "-" + p(d.getMonth() + 1)
            + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
    }

    async function composedMintDraft() {
        if (!bridge || typeof bridge.save !== "function") return false;
        // The create endpoint requires a name. If the user jumped straight to
        // the assistant without typing one, auto-fill a sensible default so the
        // first prompt actually mints a draft. Display names need not be unique
        // (the server uniquifies the filename); the user can rename later from
        // the Assets page.
        const nameEl = document.getElementById("composed-name");
        if (nameEl && !(nameEl.value || "").trim()) {
            nameEl.value = defaultSlideName();
        }
        let ok = false;
        try { ok = await bridge.save(); } catch (_) { ok = false; }
        const id = assetId();
        if (!ok || !id) return false;
        // Keep the URL consistent with a normal first save. The name + group
        // picker stay editable after mint (unified create/edit UX); the editor
        // diffs them against the persisted baseline on later manual saves, so
        // post-mint renames and group changes are honored instead of no-oping.
        try {
            history.replaceState(null, "", "/assets/" + encodeURIComponent(id) + "/composed");
        } catch (_) { /* non-fatal */ }
        return true;
    }

    // Resolve the mint strategy: an explicit cfg.mint (including null to
    // disable) wins; otherwise default to the composed-editor mint.
    const mint =
        "mint" in cfg ? cfg.mint : composedMintDraft;

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
                if (typeof mint !== "function") {
                    addMsg("error", "Save this first, then ask the assistant to make changes.");
                    return;
                }
                const minted = await mint();
                if (!minted) {
                    addMsg(
                        "error",
                        "Couldn't create the slide — see the message at the top of "
                        + "the page, then send again."
                    );
                    return;
                }
            }

            // The assistant reads server-side state, so flush any unsaved
            // manual edits first — otherwise the post-turn refresh (which
            // clears the dirty flag) would silently drop them.
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
                        if (writeTools.indexOf(name) !== -1 && !toolResultIsError(evt.data)) {
                            sawSuccessfulWrite = true;
                        }
                        break;
                    }
                    case "approval_request":
                        // editor tools never need approval; if one arrives,
                        // note it rather than blocking the drawer.
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
            refreshUsage();
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
