// Capture dashboard screenshots (light + dark) for visual review and the README.
// Usage: node e2e/screens.mjs [baseUrl] [outDir]
import { chromium } from "@playwright/test";

const base = process.argv[2] ?? "http://127.0.0.1:13001";
const out = process.argv[3] ?? "../../docs/images";

const res = await fetch(`${base}/api/v1/auth/dev-token`, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ username: "sam.sre" }),
});
const { access_token: token } = await res.json();
const incidents = await (await fetch(`${base}/api/v1/incidents?limit=50`, {
  headers: { Authorization: `Bearer ${token}` },
})).json();
const resolved = incidents.items.find((i) => i.status === "RESOLVED" && i.title !== "api test");

const browser = await chromium.launch();
for (const scheme of ["light", "dark"]) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: scheme });
  await ctx.addInitScript((t) => localStorage.setItem("sentinelops.token", t), token);
  const page = await ctx.newPage();
  const shots = [["overview", "/"], ["incidents", "/incidents"], ["experiments", "/experiments"], ["services", "/services"]];
  if (resolved) shots.push(["incident", `/incidents/${resolved.id}`]);
  for (const [name, path] of shots) {
    await page.goto(`${base}${path}`, { waitUntil: "networkidle" });
    await page.waitForTimeout(800);
    await page.screenshot({ path: `${out}/${name}-${scheme}.png`, fullPage: name === "incident" });
  }
  await ctx.close();
}
await browser.close();
console.log("screens written to", out);
