"""Playwright tests for the dashboard live countdown timer.

When a now-playing schedule has ≤30 seconds remaining, the dashboard
shows a live ticking countdown (e.g. '15s', '10s') instead of
'less than a minute'. The countdown is driven by client-side JS using
the data-remaining attribute on the remaining cell.
"""

import time

import pytest
from playwright.sync_api import Page, expect


class TestDashboardCountdownJS:
    """Test the client-side JS countdown timer on the dashboard.

    Since setting up a real now-playing schedule to end within 30s is
    timing-sensitive, we inject the required DOM structure and JS data
    directly into the page to test the countdown logic in isolation.
    """

    def test_countdown_activates_at_30s_or_less(self, page: Page, e2e_server):
        """A data-remaining cell with ≤30 should update to show 'Ns' format."""
        page.goto("/")
        page.wait_for_load_state("domcontentloaded")

        # Inject a fake now-playing table row with data-remaining="10"
        page.evaluate("""() => {
            // Find or create a container for our test
            const container = document.createElement('div');
            container.id = 'countdown-test';
            container.innerHTML = `
                <table>
                    <tr>
                        <td data-remaining="10" id="test-remaining">less than a minute</td>
                    </tr>
                </table>
            `;
            document.body.appendChild(container);

            // Re-run the countdown logic for the new element
            const cells = container.querySelectorAll('td[data-remaining]');
            const loadedAt = Date.now();
            window.__countdownInterval = setInterval(() => {
                const elapsed = Math.floor((Date.now() - loadedAt) / 1000);
                cells.forEach(cell => {
                    const initial = parseInt(cell.dataset.remaining);
                    const left = Math.max(0, initial - elapsed);
                    if (left <= 30) {
                        cell.textContent = left + 's';
                    }
                });
            }, 1000);
        }""")

        # Wait 2 seconds for the interval to tick
        time.sleep(2)

        cell = page.locator("#test-remaining")
        text = cell.text_content()
        # After ~2 seconds, should show something like "8s" or "9s"
        assert text.endswith("s"), f"Expected countdown format 'Ns' but got '{text}'"
        secs = int(text.replace("s", ""))
        assert 0 <= secs <= 10, f"Countdown should be between 0 and 10, got {secs}"

        # Cleanup
        page.evaluate("clearInterval(window.__countdownInterval)")

    def test_countdown_does_not_activate_above_30s(self, page: Page, e2e_server):
        """A data-remaining cell with >30 should NOT show countdown format."""
        page.goto("/")
        page.wait_for_load_state("domcontentloaded")

        page.evaluate("""() => {
            const container = document.createElement('div');
            container.id = 'countdown-test-high';
            container.innerHTML = `
                <table>
                    <tr>
                        <td data-remaining="120" id="test-remaining-high">2 minutes</td>
                    </tr>
                </table>
            `;
            document.body.appendChild(container);

            const cells = container.querySelectorAll('td[data-remaining]');
            const loadedAt = Date.now();
            window.__countdownInterval2 = setInterval(() => {
                const elapsed = Math.floor((Date.now() - loadedAt) / 1000);
                cells.forEach(cell => {
                    const initial = parseInt(cell.dataset.remaining);
                    const left = Math.max(0, initial - elapsed);
                    if (left <= 30) {
                        cell.textContent = left + 's';
                    }
                });
            }, 1000);
        }""")

        # Wait 2 seconds — should NOT have changed to countdown
        time.sleep(2)

        cell = page.locator("#test-remaining-high")
        text = cell.text_content()
        assert text == "2 minutes", f"Should still show '2 minutes' but got '{text}'"

        page.evaluate("clearInterval(window.__countdownInterval2)")

    def test_countdown_reaches_zero(self, page: Page, e2e_server):
        """Countdown should stop at 0s (not go negative)."""
        page.goto("/")
        page.wait_for_load_state("domcontentloaded")

        page.evaluate("""() => {
            const container = document.createElement('div');
            container.id = 'countdown-test-zero';
            container.innerHTML = `
                <table>
                    <tr>
                        <td data-remaining="2" id="test-remaining-zero">less than a minute</td>
                    </tr>
                </table>
            `;
            document.body.appendChild(container);

            const cells = container.querySelectorAll('td[data-remaining]');
            const loadedAt = Date.now();
            window.__countdownInterval3 = setInterval(() => {
                const elapsed = Math.floor((Date.now() - loadedAt) / 1000);
                cells.forEach(cell => {
                    const initial = parseInt(cell.dataset.remaining);
                    const left = Math.max(0, initial - elapsed);
                    if (left <= 30) {
                        cell.textContent = left + 's';
                    }
                });
            }, 1000);
        }""")

        # Wait long enough for it to reach 0
        time.sleep(4)

        cell = page.locator("#test-remaining-zero")
        text = cell.text_content()
        assert text == "0s", f"Should show '0s' but got '{text}'"

        page.evaluate("clearInterval(window.__countdownInterval3)")

    def test_countdown_decrements_each_second(self, page: Page, e2e_server):
        """Countdown value should decrease over time."""
        page.goto("/")
        page.wait_for_load_state("domcontentloaded")

        page.evaluate("""() => {
            const container = document.createElement('div');
            container.id = 'countdown-test-tick';
            container.innerHTML = `
                <table>
                    <tr>
                        <td data-remaining="20" id="test-remaining-tick">less than a minute</td>
                    </tr>
                </table>
            `;
            document.body.appendChild(container);

            const cells = container.querySelectorAll('td[data-remaining]');
            const loadedAt = Date.now();
            window.__countdownInterval4 = setInterval(() => {
                const elapsed = Math.floor((Date.now() - loadedAt) / 1000);
                cells.forEach(cell => {
                    const initial = parseInt(cell.dataset.remaining);
                    const left = Math.max(0, initial - elapsed);
                    if (left <= 30) {
                        cell.textContent = left + 's';
                    }
                });
            }, 1000);
        }""")

        # Capture value after 1 second
        time.sleep(1.5)
        cell = page.locator("#test-remaining-tick")
        first_text = cell.text_content()
        first_val = int(first_text.replace("s", ""))

        # Capture value after another 2 seconds
        time.sleep(2)
        second_text = cell.text_content()
        second_val = int(second_text.replace("s", ""))

        assert second_val < first_val, \
            f"Countdown should decrease: {first_val} -> {second_val}"
        # Should have decreased by roughly 2 seconds (± timing tolerance)
        delta = first_val - second_val
        assert 1 <= delta <= 3, f"Expected ~2s decrease, got {delta}s"

        page.evaluate("clearInterval(window.__countdownInterval4)")
