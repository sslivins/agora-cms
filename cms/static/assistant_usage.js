// Shared per-user assistant usage strip.
//
// One source of truth for the "Today: used / cap tok • ~$X" strip that
// appears below the composer on the main Assistant page AND inside the
// composed-slide editor's AI drawer (and any future popup assistant).
//
// Data source: GET /api/chat/usage -> { used_tokens, cap_tokens,
// used_usd_estimate, unlimited }. Failures are swallowed: the strip is
// informational, not critical, and we'd rather hide it than block the
// assistant on a transient 5xx.
//
// Usage:
//   const usage = window.AssistantUsage.create(hostEl);
//   usage.refresh();   // call on init + after every completed turn
//
// `hostEl` is any empty element; the component fills it with its own
// scoped children, so multiple instances on a page never collide on ids.
(function () {
    "use strict";

    function formatTokens(n) {
        // Compact form for the strip — keeps "1,240 / 50,000" readable.
        return Number(n || 0).toLocaleString();
    }

    function formatUsd(n) {
        const v = Number(n || 0);
        // Sub-cent: 4 decimals so $0.0015 isn't reported as $0.00.
        if (v < 0.01) return `$${v.toFixed(4)}`;
        if (v < 1) return `$${v.toFixed(3)}`;
        return `$${v.toFixed(2)}`;
    }

    // Build the strip's DOM inside `host` and return a controller with a
    // refresh() method. The host gets the `.assistant-usage` class so the
    // shared CSS (cms/static/style.css) applies wherever it's mounted.
    function create(host) {
        if (!host) return { el: null, refresh: async function () {} };

        host.classList.add("assistant-usage");
        host.style.display = "none";
        host.innerHTML =
            '<span class="assistant-usage-text">Today: …</span>' +
            '<div class="assistant-usage-bar" aria-hidden="true">' +
            '<div class="assistant-usage-fill"></div>' +
            "</div>" +
            '<span class="assistant-usage-hint" ' +
            'title="USD figure is an estimate based on the configured ' +
            "Azure OpenAI model's list price. Actual billing may differ " +
            'slightly.">est.</span>';

        const text = host.querySelector(".assistant-usage-text");
        const fill = host.querySelector(".assistant-usage-fill");

        async function refresh() {
            try {
                const r = await fetch("/api/chat/usage");
                if (!r.ok) {
                    host.style.display = "none";
                    return;
                }
                const u = await r.json();
                const used = u.used_tokens || 0;
                const cap = u.cap_tokens || 0;
                const usd = formatUsd(u.used_usd_estimate);
                if (u.unlimited) {
                    host.dataset.unlimited = "true";
                    text.textContent =
                        `Today: ${formatTokens(used)} tok • ~${usd} (no cap)`;
                    fill.style.width = "0%";
                } else if (cap === 0) {
                    // Admin-paused user.  Keep the strip but make it obvious.
                    host.dataset.unlimited = "false";
                    text.textContent =
                        `Today: ${formatTokens(used)} tok • ~${usd} (cap 0 — disabled)`;
                    fill.style.width = "100%";
                    fill.classList.remove("warn");
                    fill.classList.add("danger");
                } else {
                    host.dataset.unlimited = "false";
                    const pct = Math.min(100, (used / cap) * 100);
                    text.textContent =
                        `Today: ${formatTokens(used)} / ${formatTokens(cap)} tok • ~${usd}`;
                    fill.style.width = pct.toFixed(1) + "%";
                    fill.classList.remove("warn", "danger");
                    if (pct >= 90) fill.classList.add("danger");
                    else if (pct >= 70) fill.classList.add("warn");
                }
                host.style.display = "";
            } catch (e) {
                host.style.display = "none";
            }
        }

        return { el: host, refresh: refresh };
    }

    window.AssistantUsage = { create: create, formatTokens: formatTokens, formatUsd: formatUsd };
})();
