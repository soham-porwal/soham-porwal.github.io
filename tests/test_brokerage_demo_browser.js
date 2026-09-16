"use strict";

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const baseUrl = process.env.DEMO_SITE_URL || "http://127.0.0.1:8139/appmosis.html";
const outputDir = process.env.DEMO_SCREENSHOT_DIR || path.join(process.cwd(), "demo-api", "browser-proof");
const chrome = process.env.DEMO_CHROME || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const unavailable = process.env.DEMO_EXPECT_UNAVAILABLE === "1";

async function expectText(locator, expected) {
  await locator.filter({ hasText: expected }).waitFor({ state: "visible" });
  const actual = (await locator.textContent()).trim();
  if (!actual.includes(expected)) throw new Error(`expected ${JSON.stringify(expected)} in ${JSON.stringify(actual)}`);
}

async function submitSample(page, sample, expected) {
  await page.locator(`[data-sample="${sample}"]`).click();
  await page.locator("[data-demo-form] button[type=submit]").click();
  await expectText(page.locator("[data-demo-status] strong"), expected);
}

(async () => {
  fs.mkdirSync(outputDir, { recursive: true });
  const browser = await chromium.launch({ executablePath: chrome, headless: true });
  try {
    if (unavailable) {
      const checks = [];
      for (const [label, width, height] of [["desktop", 1440, 1000], ["mobile", 390, 844]]) {
        const page = await browser.newPage({ viewport: { width, height } });
        let normalizeRequests = 0;
        const errors = [];
        page.on("request", request => { if (new URL(request.url()).pathname === "/normalize") normalizeRequests++; });
        page.on("pageerror", () => errors.push("PAGE_ERROR"));
        const response = await page.goto(baseUrl, { waitUntil: "networkidle" });
        if (!response || response.status() !== 200) throw new Error("page unavailable");
        await expectText(page.locator("[data-demo-status] strong"), "Demo API not yet public");
        if (await page.locator(".broker-demo").getAttribute("data-api-url")) throw new Error("unexpected configured API");
        for (const sample of ["alpaca", "plaid", "fidelity", "quarantine"]) {
          await submitSample(page, sample, "Demo API unavailable");
          if (await page.locator("[data-demo-result]").isVisible()) throw new Error("fabricated result visible");
        }
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
        if (overflow || normalizeRequests || errors.length) throw new Error("unavailable-state browser guard failed");
        await page.screenshot({ path: path.join(outputDir, `${label}-unavailable.png`), fullPage: true });
        checks.push({ viewport: label, width, height, http: response.status(), samples: 4, unavailable: true, normalizeRequests, pageErrors: errors.length, overflow });
        await page.close();
      }
      const proof = { outcome: "OK", url: baseUrl, expected: "unavailable", checks };
      fs.writeFileSync(path.join(outputDir, "browser-result.json"), `${JSON.stringify(proof, null, 2)}\n`, "utf8");
      console.log(JSON.stringify(proof));
      return;
    }
    const desktop = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await desktop.goto(baseUrl, { waitUntil: "networkidle" });
    await expectText(desktop.locator("#broker-demo-title"), "Normalize a synthetic brokerage export");
    await submitSample(desktop, "alpaca", "Accepted");
    await expectText(desktop.locator("[data-demo-result]"), "sample:AAPL");
    await expectText(desktop.locator("[data-demo-result]"), "Trace ID:");
    const desktopOverflow = await desktop.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    if (desktopOverflow) throw new Error("desktop page has horizontal overflow");
    await desktop.screenshot({ path: path.join(outputDir, "desktop-accepted.png"), fullPage: true });

    await submitSample(desktop, "quarantine", "Quarantined for review");
    await expectText(desktop.locator("[data-demo-result]"), "Reason codes");

    const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1 });
    await mobile.goto(baseUrl, { waitUntil: "networkidle" });
    await submitSample(mobile, "fidelity", "Accepted");
    await expectText(mobile.locator("[data-demo-result]"), "sample:VTI");
    const mobileOverflow = await mobile.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    if (mobileOverflow) throw new Error("mobile page has horizontal overflow");
    await mobile.screenshot({ path: path.join(outputDir, "mobile-accepted.png"), fullPage: true });

    const proof = {
      outcome: "OK",
      desktop: "alpaca accepted; quarantine rendered; no horizontal overflow",
      mobile: "fidelity accepted; no horizontal overflow",
      screenshots: ["desktop-accepted.png", "mobile-accepted.png"]
    };
    fs.writeFileSync(path.join(outputDir, "browser-result.json"), `${JSON.stringify(proof, null, 2)}\n`, "utf8");
    console.log(JSON.stringify(proof));
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exitCode = 1;
});
