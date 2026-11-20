// Record one scene of the stage frame by frame: headless Edge (real GPU), window.__seek(t), JPEG frames piped into ffmpeg.
// usage: node record.mjs <scene> <out.mp4> [fps=30] [layout=landscape] [from=0] [to=duration]
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const require = createRequire(new URL("../../frontend/package.json", import.meta.url)); // Playwright from the console's dev dependencies
const { chromium } = require("@playwright/test");

const [scene, out, fpsArg, layoutArg, fromArg, toArg] = process.argv.slice(2);
const fps = Number(fpsArg || 30);
const layout = layoutArg || "landscape";
const W = layout === "portrait" ? 1080 : 1920;
const H = layout === "portrait" ? 1920 : 1080;
const here = path.dirname(fileURLToPath(import.meta.url));
const url = pathToFileURL(path.join(here, "stage", "record.html")).href + `?mode=record&scene=${scene}&layout=${layout}` + (process.env.NOCAP ? "&nocap=1" : "");

const browser = await chromium.launch({ channel: "msedge", headless: true, args: ["--allow-file-access-from-files"] });
const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
page.on("pageerror", (e) => console.error("PAGE ERROR", e.message));
page.on("console", (m) => { if (m.type() === "error") console.error("CONSOLE", m.text()); });
await page.goto(url);
await page.waitForFunction(() => window.__ready === true, null, { timeout: 30000 });
const duration = await page.evaluate(() => window.__duration);
const from = Number(fromArg || 0);
const to = Number(toArg || duration);
const frames = Math.round((to - from) * fps);

const ff = spawn("ffmpeg", ["-v", "error", "-y", "-f", "image2pipe", "-framerate", String(fps), "-i", "-",
  "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out],
  { stdio: ["pipe", "inherit", "inherit"] });
const started = Date.now();
for (let i = 0; i < frames; i++) {
  await page.evaluate((x) => window.__seek(x), from + i / fps);
  const buf = await page.screenshot({ type: "jpeg", quality: 94 });
  if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once("drain", r));
}
ff.stdin.end();
await new Promise((r) => ff.on("close", r));
await browser.close();
console.log(`${scene}: ${frames} frames (${(to - from).toFixed(1)} s) in ${((Date.now() - started) / 1000).toFixed(0)} s -> ${out}`);
