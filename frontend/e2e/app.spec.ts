import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

async function seriousViolations(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  return results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id}: ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
}

test("the first load shows real research with no clicks", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Every unusual daily move");
  await expect(page.getByRole("heading", { name: /^Why NOVA rose/ })).toBeVisible();
  await expect(page.getByRole("region", { name: "Market pulse" }).getByRole("link")).toHaveCount(6); // the sample data's last 5 sessions
  await expect(page.getByText(/Data through the close/)).toBeVisible();
  expect(await seriousViolations(page)).toEqual([]);
});

test("search finds a ticker and the keyboard walks its moves", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /^Why NOVA rose/ })).toBeVisible(); // the app is listening
  await page.keyboard.press("/");
  await page.getByPlaceholder(/NVDA, a company/).fill("nova");
  await page.getByRole("option", { name: /NOVA/ }).first().click();
  await expect(page).toHaveURL(/\/t\/NOVA$/);
  await expect(page.getByTestId("price-chart").locator("canvas").first()).toBeVisible();
  const firstRow = page.locator("tbody tr").first();
  await expect(firstRow).toHaveAttribute("aria-selected", "true");
  await page.locator("body").press("j");
  await expect(page.locator("tbody tr").nth(1)).toHaveAttribute("aria-selected", "true");
  await expect(page).toHaveURL(/m=2026-/);
  await page.locator("body").press("Enter");
  await expect(page).toHaveURL(/\/t\/NOVA\/m\/2026-/);
  expect(await seriousViolations(page)).toEqual([]);
});

test("a filter in the URL narrows the table", async ({ page }) => {
  await page.goto("/t/NOVA?direction=down");
  await expect(page.getByText(/^\d+ moves?, \d+ down/)).toBeVisible();
  const returns = await page.locator("tbody tr td:nth-child(2)").allInnerTexts();
  expect(returns.length).toBeGreaterThan(0);
  for (const r of returns) expect(r).toContain("−");
});

test("the case file explains one move and is shareable", async ({ page }) => {
  await page.goto("/t/NOVA/m/2026-10-15");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Why NOVA rose 9.0% on Oct 15");
  await expect(page.getByRole("img", { name: /Company-specific/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: "When the news broke" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Primary source" })).toBeVisible();
  await expect(page.getByText("Primary cause")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Why this was flagged" })).toBeVisible();
  expect(await page.title()).toContain("Why NOVA rose 9.0% on Oct 15");
  expect(await seriousViolations(page)).toEqual([]);
});

test("a question streams an answer and a citation opens the case file", async ({ page }) => {
  await page.goto("/ask");
  await page.getByLabel("Your question").fill("Why did NOVA rise?");
  await page.getByLabel("Your question").press("Enter");
  await expect(page.getByText(/^Checked 5 movements and 1 case file/)).toBeVisible();
  const chip = page.getByRole("link", { name: /^Source M\d+/ });
  await expect(chip).toBeVisible();
  await expect(page.getByRole("region", { name: "Sources cited" })).toContainText("Sources cited (2)");
  await expect(page.getByRole("button", { name: /Why did NOVA fall on its worst day/ })).toBeVisible();
  expect(await seriousViolations(page)).toEqual([]);
  await chip.click();
  await expect(page).toHaveURL(/\/t\/NOVA\/m\/2026-10-15/);
});

test("a limit is explained in plain words and never by its internal name", async ({ page }) => {
  await page.goto("/t/LIMIT");
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("You've hit today's free limit for new tickers. Stored research is still available, and the limit resets at");
  await expect(page.locator("body")).not.toContainText("cold_ingests");
  await alert.getByRole("button", { name: "Details" }).click();
  await expect(alert).toContainText("req-sample-429");
});

test("research progress is shown live, then the data", async ({ page }) => {
  await page.goto("/t/COLD");
  const progress = page.getByRole("region", { name: "Researching COLD" });
  await expect(progress).toBeVisible();
  await expect(progress).toContainText("Fetched 260 sessions of prices");
  await expect(progress).toContainText("Found 3 unusual moves");
  await expect(progress).toBeHidden({ timeout: 10_000 });
  await expect(page.getByTestId("price-chart")).toBeVisible();
});

test("shortcuts and the theme work from the keyboard", async ({ page }) => {
  await page.goto("/methodology");
  await expect(page.getByRole("heading", { level: 1, name: "How Metrix works" })).toBeVisible();
  await page.locator("body").press("?");
  await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.locator("body").press("t");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(await seriousViolations(page)).toEqual([]);
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });
  for (const path of ["/", "/t/NOVA", "/t/NOVA/m/2026-10-15", "/ask", "/pulse", "/methodology", "/watchlist"]) {
    test(`${path} never scrolls sideways`, async ({ page }) => {
      await page.goto(path);
      await page.waitForLoadState("networkidle");
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    });
  }
  test("the bottom tabs navigate", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("navigation", { name: "Main" }).last().getByRole("link", { name: "Ask" }).click();
    await expect(page).toHaveURL(/\/ask$/);
  });
});

test.describe("in dark mode", () => {
  test.use({ colorScheme: "dark" });
  for (const path of ["/", "/t/NOVA/m/2026-10-15"]) {
    test(`${path} has no serious accessibility violations`, async ({ page }) => {
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await page.waitForLoadState("networkidle");
      expect(await seriousViolations(page)).toEqual([]);
    });
  }
});

test("a watchlist is created, weighted and summarised", async ({ page }) => {
  await page.goto("/watchlist");
  await page.getByRole("button", { name: "Create a watchlist" }).click();
  await page.getByLabel("Add a ticker").fill("nova");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await page.getByLabel("Add a ticker").fill("KITE");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  const digest = page.getByRole("region", { name: /Over 5 sessions/ });
  await expect(digest.getByRole("link", { name: "NOVA", exact: true })).toBeVisible();
  await expect(digest.getByRole("row")).toHaveCount(3);
  await expect(digest).toContainText("50%");
});
