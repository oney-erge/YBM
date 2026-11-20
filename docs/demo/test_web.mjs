// Exercise the web player like a visitor: desktop and phone width, the approval gate, the tabs, and the page left usable at the end.
import { createRequire } from "node:module";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
const require = createRequire(new URL("../../frontend/package.json", import.meta.url)); // Playwright from the console's dev dependencies
const { chromium } = require("@playwright/test");
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, "stage");
const out = path.join(here, "out");
fs.mkdirSync(out, { recursive: true });
const types = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".ttf": "font/ttf" };
const server = http.createServer((req, res) => {
  const p = decodeURIComponent(new URL(req.url, "http://x").pathname).replace(/^\/demo\/ybm\//, "/");
  const file = path.join(root, p.endsWith("/") ? p + "index.html" : p);
  if (!fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { "content-type": types[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});
await new Promise((r) => server.listen(4180, r));
const browser = await chromium.launch({ channel: "msedge", headless: true });
const errors = [];
for (const [label, vp] of [["desktop", { width: 1280, height: 900 }], ["phone", { width: 390, height: 844 }]]) {
  const ctx = await browser.newContext({ viewport: vp, isMobile: label === "phone", hasTouch: label === "phone" });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => errors.push(`${label}: ${e.message}`));
  await page.goto("http://127.0.0.1:4180/demo/ybm/");
  await page.waitForTimeout(13000); // organize plays up to the approval and waits there
  const gate = await page.evaluate(() => ({ hint: getComputedStyle(document.querySelector(".hint")).display, sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
  await page.screenshot({ path: path.join(out, `web-${label}-gate.png`), fullPage: true });
  // tap Approve once (the real element inside the scaled stage)
  await page.locator(".approval .ok").click({ force: true });
  await page.waitForTimeout(15000);
  await page.screenshot({ path: path.join(out, `web-${label}-end.png`), fullPage: true });
  await page.locator('.tabs button[data-scene="build"]').click();
  await page.waitForTimeout(27000);
  const frame = page.frameLocator("iframe");
  await frame.locator("#billAmount").fill("120");
  await frame.locator("#tipPercentage").fill("20");
  await frame.locator("button").click();
  const result = await frame.locator("#result").innerText();
  await page.screenshot({ path: path.join(out, `web-${label}-build.png`), fullPage: true });
  console.log(label, JSON.stringify(gate), "visitor result:", result.replace(/\s+/g, " "));
  await ctx.close();
}
await browser.close();
server.close();
console.log("errors:", errors.length ? errors : "none");
