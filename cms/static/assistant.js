// Assistant chat UI — vanilla JS + fetch streaming.
//
// We use fetch() rather than EventSource because EventSource is GET-only
// and the backend's /stream endpoint takes the prompt as a POST body.
// The stream wire format is plain SSE so we parse `event: ...\n\n` frames
// off the byte stream ourselves.
//
// Event types from the backend agent (PR 3c + PR 4):
//   token             text delta
//   tool_call         { id, name, arguments }     — about to invoke
//   tool_result       { id, name, content }       — read tool finished
//   approval_request  { approval_id, name, arguments, tool_call_id }
//   done              { message_id, tokens_in, tokens_out }
//   error             { message }
(function () {
    "use strict";

    const $app = document.getElementById("assistant-app");
    const $unavailable = document.getElementById("assistant-unavailable");
    const $threadList = document.getElementById("assistant-thread-list");
    const $messages = document.getElementById("assistant-messages");
    const $input = document.getElementById("assistant-input");
    const $send = document.getElementById("assistant-send");
    const $form = document.getElementById("assistant-composer");
    const $newThread = document.getElementById("assistant-new-thread");

    // ── Markdown rendering ───────────────────────────────────────────
    // The model emits Markdown by default; render it as sanitized HTML
    // for assistant turns so headings / bullets / bold / code render
    // properly.  We keep user + tool bubbles as plain text since their
    // content isn't markdown-authored (tool output is JSON-ish).
    const _hasMarkdown =
        typeof window.marked !== "undefined" && typeof window.DOMPurify !== "undefined";
    if (_hasMarkdown) {
        // GFM on (tables, ~~strike~~), break on \n inside paragraphs.
        window.marked.setOptions({ gfm: true, breaks: true });
    }
    function renderAssistantMarkdown(bubble, text) {
        if (!_hasMarkdown) {
            bubble.textContent = text || "";
            return;
        }
        const html = window.marked.parse(text || "");
        bubble.innerHTML = window.DOMPurify.sanitize(html);
        bubble.classList.add("is-markdown");
    }

    let state = {
        threads: [],
        activeThreadId: null,
        sending: false,
        // Map approval_id -> DOM node so we can flip cards on decision.
        approvalCards: new Map(),
    };

    // ── Feature gate ─────────────────────────────────────────────────
    async function probeFeature() {
        try {
            const r = await fetch("/api/chat/feature");
            if (!r.ok) return false;
            const j = await r.json();
            return !!j.enabled;
        } catch (e) { return false; }
    }

    // ── API helpers ──────────────────────────────────────────────────
    async function apiGet(path) {
        const r = await fetch(path);
        if (!r.ok) throw new Error(`${path} → ${r.status}`);
        return r.json();
    }
    async function apiPost(path, body) {
        const r = await fetch(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {}),
        });
        if (!r.ok) {
            const text = await r.text();
            throw new Error(`${path} → ${r.status}: ${text}`);
        }
        return r.json();
    }
    async function apiDelete(path) {
        const r = await fetch(path, { method: "DELETE" });
        if (!r.ok && r.status !== 204) throw new Error(`${path} → ${r.status}`);
    }

    // ── Thread sidebar ───────────────────────────────────────────────
    async function loadThreads() {
        state.threads = await apiGet("/api/chat/threads");
        renderThreads();
    }
    function renderThreads() {
        $threadList.innerHTML = "";
        if (!state.threads.length) {
            const p = document.createElement("p");
            p.className = "assistant-empty-state";
            p.textContent = "No conversations yet.";
            $threadList.appendChild(p);
            return;
        }
        state.threads.forEach((t) => {
            const row = document.createElement("div");
            row.className = "assistant-thread";
            if (t.id === state.activeThreadId) row.classList.add("active");
            row.dataset.threadId = t.id;

            const title = document.createElement("span");
            title.className = "assistant-thread-title";
            title.textContent = t.title || "(new conversation)";
            row.appendChild(title);

            const del = document.createElement("button");
            del.className = "assistant-thread-delete";
            del.title = "Delete thread";
            del.textContent = "×";
            del.addEventListener("click", async (ev) => {
                ev.stopPropagation();
                const ok = await showConfirm("Delete this conversation?");
                if (!ok) return;
                await apiDelete(`/api/chat/threads/${t.id}`);
                if (state.activeThreadId === t.id) {
                    state.activeThreadId = null;
                    renderEmptyConversation();
                }
                await loadThreads();
            });
            row.appendChild(del);

            row.addEventListener("click", () => selectThread(t.id));
            $threadList.appendChild(row);
        });
    }

    async function createThread() {
        const created = await apiPost("/api/chat/threads", { title: "" });
        await loadThreads();
        await selectThread(created.id);
        $input.focus();
    }

    async function selectThread(id) {
        state.activeThreadId = id;
        state.approvalCards.clear();
        renderThreads();
        await loadConversation(id);
        $input.disabled = false;
        $send.disabled = false;
    }

    function renderEmptyConversation() {
        $messages.innerHTML = "";
        const p = document.createElement("p");
        p.className = "assistant-empty-state";
        p.textContent = "Pick a thread or start a new one.";
        $messages.appendChild(p);
        $input.disabled = true;
        $send.disabled = true;
    }

    // ── Conversation view ────────────────────────────────────────────
    async function loadConversation(id) {
        const data = await apiGet(`/api/chat/threads/${id}/messages`);
        $messages.innerHTML = "";
        const approvals = await loadApprovalIndex(id);
        data.forEach((m) => appendMessage(m, approvals));
        scrollToBottom();
    }

    async function loadApprovalIndex(threadId) {
        // Fetch ALL approvals (any state) and map by tool_call_id so
        // we can render the right card on placeholder tool rows.
        try {
            const list = await apiGet(
                `/api/chat/threads/${threadId}/approvals?status=`
            );
            const idx = new Map();
            list.forEach((a) => idx.set(a.tool_call_id, a));
            return idx;
        } catch (e) {
            console.warn("approvals fetch failed", e);
            return new Map();
        }
    }

    function appendMessage(m, approvalIndex) {
        // Tool rows that are placeholders (awaiting_approval) render
        // as the approval card so the user can act on them on reload.
        if (m.role === "tool") {
            let parsed = null;
            try { parsed = JSON.parse(m.content); } catch (e) {}
            if (parsed && parsed.status === "awaiting_approval"
                && approvalIndex && approvalIndex.has(m.tool_call_id)) {
                const approval = approvalIndex.get(m.tool_call_id);
                renderApprovalCard(approval);
                return;
            }
            // A finished approval has the real result content — render
            // the historical decision instead of the raw blob.
            if (approvalIndex && approvalIndex.has(m.tool_call_id)) {
                const approval = approvalIndex.get(m.tool_call_id);
                if (approval.status === "approved" || approval.status === "rejected") {
                    renderApprovalCard(approval);
                    return;
                }
            }
        }
        const wrap = document.createElement("div");
        wrap.className = `assistant-message ${m.role}`;
        const bubble = document.createElement("div");
        bubble.className = "assistant-message-bubble";

        if (m.role === "assistant" && m.tool_calls && m.tool_calls.length) {
            // Assistant turn that was a tool-call announcement.
            const list = m.tool_calls
                .map((tc) => `→ ${tc.function?.name || "?"}(${tc.function?.arguments || ""})`)
                .join("\n");
            bubble.textContent = m.content
                ? `${m.content}\n\n${list}`
                : list;
        } else if (m.role === "assistant") {
            renderAssistantMarkdown(bubble, m.content || "");
        } else {
            bubble.textContent = m.content || "";
        }
        wrap.appendChild(bubble);
        $messages.appendChild(wrap);
    }

    // ── Approval card ────────────────────────────────────────────────
    function renderApprovalArgs(parent, toolArgs) {
        // Render the tool's arguments as a readable key/value list
        // instead of a raw JSON blob.  Most write tools take a flat
        // bag of scalars (name, start_time, asset_id, …); arrays and
        // nested objects collapse to a one-line pretty-printed form.
        const wrap = document.createElement("div");
        wrap.className = "assistant-approval-args";
        const entries = toolArgs && typeof toolArgs === "object"
            ? Object.entries(toolArgs)
            : [];
        if (entries.length === 0) {
            const empty = document.createElement("span");
            empty.className = "empty";
            empty.textContent = "(no arguments)";
            wrap.appendChild(empty);
            parent.appendChild(wrap);
            return;
        }
        const dl = document.createElement("dl");
        for (const [key, value] of entries) {
            const dt = document.createElement("dt");
            dt.textContent = key;
            const dd = document.createElement("dd");
            const { text, multiline } = formatApprovalValue(value);
            dd.textContent = text;
            if (multiline) dd.classList.add("is-multiline");
            dl.appendChild(dt);
            dl.appendChild(dd);
        }
        wrap.appendChild(dl);
        parent.appendChild(wrap);
    }

    function formatApprovalValue(v) {
        if (v === null || v === undefined) return { text: "—", multiline: false };
        if (typeof v === "boolean") return { text: v ? "yes" : "no", multiline: false };
        if (typeof v === "number" || typeof v === "string") {
            const s = String(v);
            return { text: s, multiline: s.includes("\n") };
        }
        if (Array.isArray(v)) {
            // Short arrays of scalars → "a, b, c"; everything else → JSON.
            const allScalar = v.every(
                (x) => x === null || ["string", "number", "boolean"].includes(typeof x)
            );
            if (allScalar && v.length <= 12) {
                return { text: v.length === 0 ? "(none)" : v.join(", "), multiline: false };
            }
            const j = JSON.stringify(v, null, 2);
            return { text: j, multiline: true };
        }
        // Object — pretty print on multiple lines.
        const j = JSON.stringify(v, null, 2);
        return { text: j, multiline: true };
    }

    function renderApprovalCard(approval) {
        const card = document.createElement("div");
        card.className = "assistant-approval";
        if (approval.status !== "pending") card.classList.add("decided");
        card.dataset.approvalId = approval.id;

        const h = document.createElement("h4");
        h.textContent = `⚠ Approval needed — ${approval.tool_name}`;
        card.appendChild(h);

        renderApprovalArgs(card, approval.tool_arguments);

        const actions = document.createElement("div");
        actions.className = "assistant-approval-actions";
        const approveBtn = document.createElement("button");
        approveBtn.className = "btn-approve";
        approveBtn.textContent = "Approve & run";
        approveBtn.addEventListener("click", () => decideApproval(approval.id, "approve", card));
        const rejectBtn = document.createElement("button");
        rejectBtn.className = "btn-reject";
        rejectBtn.textContent = "Reject";
        rejectBtn.addEventListener("click", () => decideApproval(approval.id, "reject", card));
        actions.appendChild(approveBtn);
        actions.appendChild(rejectBtn);
        card.appendChild(actions);

        const decision = document.createElement("div");
        decision.className = "assistant-approval-decision";
        if (approval.status === "approved") decision.textContent = "✓ Approved";
        else if (approval.status === "rejected") decision.textContent = "✗ Rejected";
        else if (approval.status === "expired") decision.textContent = "⏳ Expired";
        card.appendChild(decision);

        $messages.appendChild(card);
        state.approvalCards.set(approval.id, card);
    }

    async function decideApproval(approvalId, action, card) {
        let note = "";
        if (action === "reject") {
            const entered = await showPrompt("Optional reason for rejecting:");
            // Null = cancel — bail without sending the decision.
            if (entered === null) return;
            note = entered;
        }
        const buttons = card.querySelectorAll("button");
        buttons.forEach((b) => b.disabled = true);
        try {
            const result = await apiPost(
                `/api/chat/approvals/${approvalId}/${action}`,
                { note }
            );
            card.classList.add("decided");
            const decision = card.querySelector(".assistant-approval-decision");
            if (decision) {
                decision.textContent = action === "approve"
                    ? `✓ Approved — result attached to thread. Send a message to continue.`
                    : `✗ Rejected${note ? " — " + note : ""}`;
            }
        } catch (e) {
            showToast(`Decision failed: ${e.message}`, "error");
            buttons.forEach((b) => b.disabled = false);
        }
    }

    // ── Send + stream ────────────────────────────────────────────────
    async function sendMessage(content) {
        if (state.sending) return;
        if (!state.activeThreadId) {
            await createThread();
        }
        state.sending = true;
        $send.disabled = true;
        $input.disabled = true;

        // Optimistic user-bubble render.
        appendMessage({ role: "user", content }, null);
        const indicator = document.createElement("div");
        indicator.className = "assistant-streaming-indicator";
        indicator.textContent = "Thinking ";
        $messages.appendChild(indicator);
        scrollToBottom();

        let assistantBubble = null;
        let assistantBuffer = "";
        const ensureAssistantBubble = () => {
            if (assistantBubble) return assistantBubble;
            const wrap = document.createElement("div");
            wrap.className = "assistant-message assistant";
            const b = document.createElement("div");
            b.className = "assistant-message-bubble";
            wrap.appendChild(b);
            $messages.insertBefore(wrap, indicator);
            assistantBubble = b;
            return b;
        };

        try {
            const resp = await fetch(
                `/api/chat/threads/${state.activeThreadId}/stream`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ content }),
                }
            );
            if (!resp.ok) {
                const errText = await resp.text();
                throw new Error(`stream → ${resp.status}: ${errText}`);
            }
            await consumeSseStream(resp.body, (evt) => {
                switch (evt.event) {
                    case "token":
                        assistantBuffer += evt.data.text || "";
                        renderAssistantMarkdown(ensureAssistantBubble(), assistantBuffer);
                        scrollToBottom();
                        break;
                    case "tool_call": {
                        const note = document.createElement("div");
                        note.className = "assistant-tool-call";
                        note.textContent = `→ calling ${evt.data.name}…`;
                        $messages.insertBefore(note, indicator);
                        scrollToBottom();
                        break;
                    }
                    case "tool_result": {
                        const note = document.createElement("div");
                        note.className = "assistant-tool-call";
                        note.textContent = `✓ ${evt.data.name} done`;
                        $messages.insertBefore(note, indicator);
                        scrollToBottom();
                        break;
                    }
                    case "approval_request": {
                        // Build a minimal ChatPendingApprovalOut-shaped object.
                        const approval = {
                            id: evt.data.approval_id,
                            tool_call_id: evt.data.tool_call_id,
                            tool_name: evt.data.name,
                            tool_arguments: evt.data.arguments,
                            status: "pending",
                        };
                        // Use a temporary card host BEFORE the indicator.
                        const card = renderApprovalCardInline(approval, indicator);
                        scrollToBottom();
                        break;
                    }
                    case "done":
                        break;
                    case "error": {
                        const e = document.createElement("div");
                        e.className = "assistant-error";
                        e.textContent = `Error: ${evt.data.message || "unknown"}`;
                        $messages.insertBefore(e, indicator);
                        scrollToBottom();
                        break;
                    }
                }
            });
        } catch (e) {
            const errBox = document.createElement("div");
            errBox.className = "assistant-error";
            errBox.textContent = `Request failed: ${e.message}`;
            $messages.insertBefore(errBox, indicator);
        } finally {
            indicator.remove();
            state.sending = false;
            $send.disabled = false;
            $input.disabled = false;
            $input.focus();
            // Refresh sidebar so the auto-title shows up on a new thread.
            await loadThreads();
        }
    }

    function renderApprovalCardInline(approval, anchorBefore) {
        const card = document.createElement("div");
        card.className = "assistant-approval";
        card.dataset.approvalId = approval.id;

        const h = document.createElement("h4");
        h.textContent = `⚠ Approval needed — ${approval.tool_name}`;
        card.appendChild(h);

        renderApprovalArgs(card, approval.tool_arguments);

        const actions = document.createElement("div");
        actions.className = "assistant-approval-actions";
        const approveBtn = document.createElement("button");
        approveBtn.className = "btn-approve";
        approveBtn.textContent = "Approve & run";
        approveBtn.addEventListener("click", () => decideApproval(approval.id, "approve", card));
        const rejectBtn = document.createElement("button");
        rejectBtn.className = "btn-reject";
        rejectBtn.textContent = "Reject";
        rejectBtn.addEventListener("click", () => decideApproval(approval.id, "reject", card));
        actions.appendChild(approveBtn);
        actions.appendChild(rejectBtn);
        card.appendChild(actions);

        const decision = document.createElement("div");
        decision.className = "assistant-approval-decision";
        card.appendChild(decision);

        if (anchorBefore && anchorBefore.parentNode) {
            anchorBefore.parentNode.insertBefore(card, anchorBefore);
        } else {
            $messages.appendChild(card);
        }
        state.approvalCards.set(approval.id, card);
        return card;
    }

    // ── SSE byte-stream parser ───────────────────────────────────────
    async function consumeSseStream(stream, onEvent) {
        const reader = stream.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            // SSE frames are separated by a blank line. CRLF or LF.
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
        try { return { event, data: JSON.parse(data) }; }
        catch (e) { return { event, data: { _raw: data } }; }
    }

    // ── Misc ─────────────────────────────────────────────────────────
    function scrollToBottom() {
        $messages.scrollTop = $messages.scrollHeight;
    }

    // ── Init ─────────────────────────────────────────────────────────
    async function init() {
        const enabled = await probeFeature();
        if (!enabled) {
            $unavailable.style.display = "";
            $app.style.display = "none";
            return;
        }
        $unavailable.style.display = "none";
        $app.style.display = "";

        await loadThreads();
        renderEmptyConversation();

        $newThread.addEventListener("click", () => createThread());
        $form.addEventListener("submit", (ev) => {
            ev.preventDefault();
            const text = $input.value.trim();
            if (!text || state.sending) return;
            $input.value = "";
            $input.style.height = "auto";
            sendMessage(text);
        });
        $input.addEventListener("keydown", (ev) => {
            if (ev.key === "Enter" && !ev.shiftKey) {
                ev.preventDefault();
                $form.requestSubmit();
            }
        });
        $input.addEventListener("input", () => {
            $input.style.height = "auto";
            $input.style.height = Math.min($input.scrollHeight, 180) + "px";
        });
    }

    document.addEventListener("DOMContentLoaded", init);
})();
