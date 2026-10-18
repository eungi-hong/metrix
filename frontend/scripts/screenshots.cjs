/* eslint-disable */
// Capture docs/assets/after/ from the production bundle with the mocked sample data:
//   VITE_MSW=1 VITE_METRIX_API_BASE_URL=http://api.test npx vite build --outDir dist-e2e
//   VITE_METRIX_API_BASE_URL=http://api.test npx vite preview --outDir dist-e2e --port 4175 &
//   BASE=http://127.0.0.1:4175 npm run screenshots
// CHROMIUM_PATH picks a browser other than Playwright's own.
const path = require("node:path");
const { chromium } = require("@playwright/test");
const OUT = path.resolve(__dirname, "../../docs/assets/after");
const BASE = process.env.BASE || "http://127.0.0.1:4175";
const WIDTHS = [[1440, 900], [768, 1024], [390, 844]];

const states = [
  ["home", "/", async (p) => p.getByRole("heading", { level: 1 }).first().waitFor()],
  ["pulse", "/pulse", async (p) => p.getByRole("heading", { level: 1 }).first().waitFor()],
  ["ticker", "/t/NOVA", async (p) => { await p.getByTestId("price-chart").waitFor(); await p.waitForTimeout(600); }],
  ["case", "/t/NOVA/m/2026-10-15", async (p) => p.getByRole("heading", { level: 1 }).first().waitFor()],
  ["researching", "/t/COLD", async (p) => { await p.getByText("Found 3 unusual moves").waitFor(); }],
  ["ask", "/ask", async (p) => {
    await p.getByLabel("Your question").fill("Why did NOVA rise?");
    await p.getByLabel("Your question").press("Enter");
    await p.getByRole("region", { name: "Sources cited" }).waitFor();
    await p.waitForTimeout(300);
  }],
  ["limit", "/t/LIMIT", async (p) => p.getByRole("alert").waitFor()],
];

(async () => {
  const browser = await chromium.launch({ args: ["--no-sandbox"], executablePath: process.env.CHROMIUM_PATH || undefined });
  for (const [width, height] of WIDTHS) {
    for (const scheme of ["light", "dark"]) {
      if (scheme === "dark" && width !== 1440) continue;
      for (const [name, path, ready] of states) {
        if (scheme === "dark" && !["ticker", "case"].includes(name)) continue;
        const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme, locale: "en-US", timezoneId: "America/New_York", reducedMotion: "reduce" });
        const page = await context.newPage();
        await page.goto(BASE + path);
        await ready(page);
        await page.evaluate(() => document.fonts.ready);
        // Full-page capture draws fixed bars at their viewport position, mid-page.
        // Pin a fixed bottom bar (the phone tab bar) to the end of the document instead.
        if (!(name === "ask" && width < 768)) await page.evaluate(() => {
          const height = document.documentElement.scrollHeight;
          document.documentElement.style.position = "relative";
          for (const el of document.querySelectorAll("body *")) {
            const cs = getComputedStyle(el);
            if (cs.position === "fixed" && cs.bottom === "0px" && cs.display !== "none") {
              el.style.position = "absolute";
              el.style.top = `${height - el.offsetHeight}px`;
              el.style.bottom = "auto";
            }
          }
        });
        const file = `${OUT}/${name}${scheme === "dark" ? "-dark" : ""}-${width}.png`;
        // The Ask composer is sticky: on a phone, show the viewport scrolled to the end, as a user sees it.
        const phoneAsk = name === "ask" && width < 768;
        if (phoneAsk) { await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight)); await page.waitForTimeout(200); }
        await page.screenshot({ path: file, fullPage: !phoneAsk });
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        console.log(file, overflow > 0 ? `OVERFLOW ${overflow}px` : "ok");
        await context.close();
      }
    }
  }
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
