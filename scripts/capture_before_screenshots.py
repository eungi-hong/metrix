#!/usr/bin/env python3
"""Capture the v1 frontend's "before" screenshots, from its demo-fixture mode.

Phase 0 of v2 records how the interface looks today, so the redesign can be
shown next to it. The live demo is one source of that record; this script is
the reproducible one. It drives the built frontend through the local fixture
data that **View demo data** loads, so it needs no API, no keys and no
network, and two runs produce the same screens.

It captures six states at three widths (1440, 768 and 390 px):

    home            the first load, before any click
    ticker-loaded   after View demo data, on the Overview tab
    movements       the Movements tab
    news-pending    the News tab for the default selection, the latest move,
                    whose evidence is still pending
    news            the News tab for a move with linked evidence
    chat            Ask Metrix with one answer (the fixture answer)

Run it against a production build served by `vite preview`:

    cd frontend
    export VITE_METRIX_API_BASE_URL=http://127.0.0.1:9  # preview checks it too
    npm run build
    npx vite preview --host 127.0.0.1 --port 4173 --strictPort &
    cd ..
    pip install playwright==1.56.0 && playwright install chromium
    python scripts/capture_before_screenshots.py --base-url http://127.0.0.1:4173

The API URL is deliberately unreachable: demo mode never calls it, and a
request that did would fail loudly instead of reaching a real service.

What the screens do not show
----------------------------
* **The price chart is empty below 1024 px, and that is the app, not this
  script.** Under Tailwind's `lg` breakpoint the chart's wrapper gets its
  height only from `min-h-[300px]`, and Recharts' `ResponsiveContainer`
  (`height="100%"`) cannot resolve a percentage against a min-height, so it
  measures 0 px and draws no SVG, even when scrolled into view. The script
  prints, per width, whether the chart's SVG exists, so the blank box in the
  768 and 390 px captures is recorded as a finding rather than mistaken for a
  capture artifact.
* **The chat answer is the frontend's canned fixture**, a short plain-text
  reply with three sources. The live API's problems (raw markdown, a source
  chip for every retrieved movement, no streaming) need a model and are not
  reproduced here.

The selectors follow the v1 components (`App.tsx`, `ResearchTabs`,
`MovementsPanel`, `ChatPanel`). This script records the v1 interface; it is
not meant to drive v2.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

# (name, viewport width, viewport height): desktop, tablet, phone.
VIEWPORTS: tuple[tuple[str, int, int], ...] = (
    ("1440", 1440, 900),
    ("768", 768, 1024),
    ("390", 390, 844),
)

# Pinned so the "Stored <time>" line and date labels render the same anywhere.
LOCALE = "en-US"
TIMEZONE = "America/New_York"

# Recharts animates the line in with JavaScript, which Playwright's
# `animations="disabled"` does not stop; give it time to finish drawing.
CHART_SETTLE_MS = 1500

# The fixture movement with complete, multi-tier evidence (fixtures/demo.ts).
EVIDENCE_MOVE_LABEL = "Jul 9, 2026"
DEMO_QUESTION = "What drove the largest decline?"

# The SVG Recharts draws inside the chart section; absent when it measured 0 px.
CHART_SVG = "[aria-label='Price research chart'] svg.recharts-surface"


def _shot(page: Page, out_dir: Path, state: str, width: str) -> Path:
    path = out_dir / f"{state}-{width}.png"
    page.screenshot(path=str(path), full_page=True, animations="disabled")
    return path


def _chart_rendered(page: Page) -> bool:
    """Whether the price chart drew anything. Reads the DOM only."""
    return page.locator(CHART_SVG).count() > 0


def capture(base_url: str, out_dir: Path) -> tuple[list[Path], dict[str, bool]]:
    """Every state at every width.

    Returns the files written, in order, and whether the price chart rendered
    at each width (after View demo data).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    chart_rendered: dict[str, bool] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for width_name, width, height in VIEWPORTS:
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    locale=LOCALE,
                    timezone_id=TIMEZONE,
                    reduced_motion="reduce",
                )
                page = context.new_page()
                page.goto(base_url, wait_until="networkidle")

                view_demo = page.get_by_role("button", name="View demo data")
                expect(view_demo).to_be_visible()
                written.append(_shot(page, out_dir, "home", width_name))

                view_demo.click()
                expect(page.get_by_role("heading", name="NVIDIA Corporation")).to_be_visible()
                page.wait_for_timeout(CHART_SETTLE_MS)
                written.append(_shot(page, out_dir, "ticker-loaded", width_name))
                chart_rendered[width_name] = _chart_rendered(page)

                page.get_by_role("tab", name="Movements").click()
                expect(page.locator("#movements-panel")).to_be_visible()
                written.append(_shot(page, out_dir, "movements", width_name))

                page.get_by_role("tab", name="News").click()
                expect(page.get_by_text("Evidence is not linked yet")).to_be_visible()
                written.append(_shot(page, out_dir, "news-pending", width_name))

                page.get_by_role("tab", name="Movements").click()
                page.locator("#movements-panel button", has_text=EVIDENCE_MOVE_LABEL).click()
                expect(page.get_by_text(f"Evidence for {EVIDENCE_MOVE_LABEL}")).to_be_visible()
                page.wait_for_timeout(CHART_SETTLE_MS)
                written.append(_shot(page, out_dir, "news", width_name))

                page.get_by_role("button", name="Ask Metrix").click()
                page.locator("#metrix-question").fill(DEMO_QUESTION)
                page.get_by_role("button", name="Send question").click()
                expect(page.get_by_text("Grounded in stored evidence")).to_be_visible()
                written.append(_shot(page, out_dir, "chat", width_name))

                context.close()
        finally:
            browser.close()
    return written, chart_rendered


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:4173")
    parser.add_argument("--out", type=Path, default=Path("docs/assets/before"))
    args = parser.parse_args()
    written, chart_rendered = capture(args.base_url, args.out)
    for path in written:
        print(path)
    for width, rendered in chart_rendered.items():
        print(f"price chart rendered at {width} px: {'yes' if rendered else 'no'}")


if __name__ == "__main__":
    main()
