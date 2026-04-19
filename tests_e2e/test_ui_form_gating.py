"""Playwright tests for required-input submit-button gating.

Covers the shared `bindFormsRequiredGating` wiring in ``cms/static/app.js``:

1. Submit button starts disabled when required fields are empty.
2. Button becomes enabled once all required fields are filled via normal input.
3. Browser-autofill path (issue #348): Chromium autofill silently sets
   ``input.value`` without emitting ``input``/``change``. We hook
   ``animationstart`` on a CSS-animation tied to the ``:-webkit-autofill``
   pseudo-class to re-evaluate the gate. This test simulates that by
   assigning ``.value`` directly and dispatching a synthetic
   ``animationstart`` with the expected ``animationName``.
4. Video Stream card: the "capture duration" select is marked required
   dynamically when Save-locally is on and the probe reports a live
   stream. The gate must re-evaluate when ``required`` flips.
"""

from playwright.sync_api import Page, expect


# ── Schedules page ────────────────────────────────────────────────────────

class TestScheduleFormGating:
    def test_submit_disabled_on_load(self, page: Page, api, base_url: str):
        # Schedule form only renders when both a group and a ready asset exist.
        api.create_asset("gate-disabled-test.mp4")
        api.post("/api/devices/groups/", json={"name": "Gate-Disabled-Group"})

        page.goto(f"{base_url}/schedules")
        btn = page.locator("form[data-gate-required] button[type='submit']").first
        expect(btn).to_be_disabled()

    def test_submit_enables_after_filling_required(
        self, page: Page, api, base_url: str
    ):
        # Need at least one asset and one group so the selects have options.
        api.create_asset("gate-test.mp4")
        api.post("/api/devices/groups/", json={"name": "Gate-Test-Group"})

        page.goto(f"{base_url}/schedules")
        form = page.locator("form[data-gate-required]").first
        btn = form.locator("button[type='submit']")
        expect(btn).to_be_disabled()

        page.fill('input[name="name"]', "Gating Test")
        # Pick the first non-placeholder option for each select.
        page.evaluate("""
            () => {
                const form = document.querySelector('form[data-gate-required]');
                for (const sel of form.querySelectorAll('select')) {
                    const opt = Array.from(sel.options).find(o => o.value);
                    if (opt) { sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {bubbles: true})); }
                }
            }
        """)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "17:00")

        expect(btn).to_be_enabled()

    def test_autofill_triggers_gate_reevaluation(
        self, page: Page, api, base_url: str
    ):
        """Autofill sets .value without events; animationstart hook must refresh."""
        api.create_asset("autofill-test.mp4")
        api.post("/api/devices/groups/", json={"name": "Autofill-Test-Group"})

        page.goto(f"{base_url}/schedules")
        form = page.locator("form[data-gate-required]").first
        btn = form.locator("button[type='submit']")
        expect(btn).to_be_disabled()

        # Silently mutate every required field (bypasses input/change events).
        page.evaluate("""
            () => {
                const form = document.querySelector('form[data-gate-required]');
                for (const el of form.querySelectorAll('[required]')) {
                    if (el.tagName === 'SELECT') {
                        const opt = Array.from(el.options).find(o => o.value);
                        if (opt) el.value = opt.value;
                    } else if (el.type === 'time') {
                        el.value = '12:00';
                    } else {
                        el.value = 'autofilled';
                    }
                }
            }
        """)

        # Sanity: without events the button must still be disabled.
        expect(btn).to_be_disabled()

        # Now fire the autofill animation hook.
        page.evaluate("""
            () => {
                const form = document.querySelector('form[data-gate-required]');
                form.dispatchEvent(new AnimationEvent('animationstart', {
                    animationName: 'onAutoFillStart',
                    bubbles: true,
                }));
            }
        """)
        expect(btn).to_be_enabled()


# ── Video Stream card (Assets page) ───────────────────────────────────────

class TestStreamFormGating:
    def test_submit_disabled_on_load(self, page: Page, base_url: str):
        page.goto(f"{base_url}/assets")
        page.evaluate("switchAddTab('stream')")
        btn = page.locator("#stream-submit")
        expect(btn).to_be_disabled()

    def test_submit_enables_after_url_filled(self, page: Page, base_url: str):
        page.goto(f"{base_url}/assets")
        page.evaluate("switchAddTab('stream')")
        btn = page.locator("#stream-submit")
        expect(btn).to_be_disabled()
        page.fill("#stream-url", "https://example.com/live.m3u8")
        expect(btn).to_be_enabled()

    def test_dynamically_required_field_regates_button(
        self, page: Page, base_url: str
    ):
        """Regression test for issue #348.

        The Video Stream card makes ``#stream-capture-duration`` required
        dynamically (via ``onSaveLocallyChanged`` when save-locally is on
        and the probe reports a live stream). The shared gate originally
        snapshotted the required set once at DOMContentLoaded and missed
        later additions. The form-level delegation fix re-evaluates on
        every input/change/animation, so a field flipped to
        ``required=true`` programmatically must immediately re-disable
        the submit button.
        """
        page.goto(f"{base_url}/assets")
        page.evaluate("switchAddTab('stream')")
        btn = page.locator("#stream-submit")

        # Fill URL (only statically-required field) → button enables.
        page.fill("#stream-url", "https://example.com/live.m3u8")
        expect(btn).to_be_enabled()

        # Programmatically mark the duration select as required and
        # trigger the same refresh path the real onSaveLocallyChanged
        # uses (which doesn't emit input/change). Also reveal the
        # parent group so select_option can interact with it.
        page.evaluate("""
            () => {
                document.getElementById('stream-duration-group').style.display = 'block';
                document.getElementById('stream-capture-duration').required = true;
                document.getElementById('stream-form').__gateUpdate();
            }
        """)
        expect(btn).to_be_disabled()

        # Picking a duration should re-enable via a normal change event.
        page.select_option("#stream-capture-duration", value="300")
        expect(btn).to_be_enabled()
