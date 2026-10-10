// Record the README demo: scenario A end to end through the real dashboard.
// Usage (stack running, dev login enabled): node e2e/demo.mjs [baseUrl] [outDir]
// Writes <outDir>/demo.webm and <outDir>/demo-segments.json (which spans are waiting,
// so scripts/make_demo_media.sh can speed them up).
import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { chromium } from "@playwright/test";

const base = process.argv[2] ?? "http://127.0.0.1:13001";
const out = process.argv[3] ?? "../../.cache/demo";
const PROM = "http://127.0.0.1:19090";
const FINGERPRINT = "local:checkout:CheckoutHigh5xxRate";
mkdirSync(out, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function token(username) {
  const res = await fetch(`${base}/api/v1/auth/dev-token`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username }),
  });
  return (await res.json()).access_token;
}
const tokens = Object.fromEntries(await Promise.all(
  ["ada.admin", "oscar.operator", "anna.approver"].map(async (u) => [u, await token(u)]),
));
const api = async (path, init = {}) => {
  const res = await fetch(`${base}${path}`, {
    ...init,
    headers: { Authorization: `Bearer ${tokens["oscar.operator"]}`, "Content-Type": "application/json", ...init.headers },
  });
  return res.json();
};
async function waitFor(desc, fn, timeoutMs, everyMs = 2000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const v = await fn();
    if (v) return v;
    await sleep(everyMs);
  }
  throw new Error(`timed out waiting for ${desc}`);
}

// Pre-flight: no leftover alert or open incident for this scenario.
const alertActive = async () => {
  const q = encodeURIComponent(`ALERTS{alertname="CheckoutHigh5xxRate"}`);
  return (await (await fetch(`${PROM}/api/v1/query?query=${q}`)).json()).data.result.length > 0;
};
await waitFor("alert to clear", async () => !(await alertActive()), 600_000, 10_000);
for (const inc of (await api("/api/v1/incidents?limit=200")).items) {
  if (inc.fingerprint === FINGERPRINT && inc.status !== "RESOLVED") {
    await api(`/api/v1/incidents/${inc.id}/resolve`, { method: "POST", body: JSON.stringify({ reason: "demo pre-flight" }) });
  }
}

const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: 1280, height: 800 },
  recordVideo: { dir: out, size: { width: 1280, height: 800 } },
  colorScheme: "light",
});
await ctx.addInitScript((t) => { if (!localStorage.getItem("sentinelops.token")) localStorage.setItem("sentinelops.token", t); }, tokens["ada.admin"]);
const page = await ctx.newPage();
const t0 = Date.now();
const segments = [];
let fastFrom = null;
const fast = () => { fastFrom = (Date.now() - t0) / 1000; };
const normal = () => {
  if (fastFrom !== null) segments.push({ start: fastFrom, end: (Date.now() - t0) / 1000 });
  fastFrom = null;
};

async function caption(text) {
  await page.evaluate((txt) => {
    let el = document.getElementById("demo-caption");
    if (!el) {
      el = document.createElement("div");
      el.id = "demo-caption";
      Object.assign(el.style, {
        position: "fixed", left: "50%", bottom: "24px", transform: "translateX(-50%)", zIndex: 9999,
        background: "rgba(17,24,39,.92)", color: "#fff", padding: "12px 20px", borderRadius: "10px",
        font: "600 18px/1.35 -apple-system, system-ui, sans-serif", maxWidth: "1100px", textAlign: "center",
        boxShadow: "0 6px 24px rgba(0,0,0,.25)",
      });
      document.body.appendChild(el);
    }
    el.textContent = txt;
  }, text);
}
async function goto(path, text) {
  await page.goto(`${base}${path}`, { waitUntil: "networkidle" });
  // Never caption a half-loaded page: wait for the live stream and, on an incident, its data.
  await page.getByTestId("stream-state").filter({ hasText: "live" }).waitFor({ timeout: 15_000 });
  if (path.startsWith("/incidents/")) await page.getByText("Runbook").first().waitFor({ timeout: 15_000 });
  if (text) await caption(text);
}
async function switchUser(username, path, text) {
  await page.evaluate((t) => localStorage.setItem("sentinelops.token", t), tokens[username]);
  await goto(path, text);
}

// 1. Break something.
await goto("/", "SentinelOps watches a small set of services: checkout, inventory, payment, worker");
await sleep(3500);
await goto("/experiments", "Step 1 · Break something: roll out checkout with a malformed payment URL");
await sleep(2500);
await page.locator(".card", { hasText: /^A ·/ }).getByRole("button", { name: "Inject" }).click();
await sleep(2500);

// 2. Detection.
await goto("/incidents", "Step 2 · Prometheus sees checkout 5xx errors → Alertmanager → a new incident (≈2 min, sped up)");
fast();
const inc = await waitFor("incident", async () =>
  (await api("/api/v1/incidents?limit=200")).items.find((i) => i.fingerprint === FINGERPRINT && i.status !== "RESOLVED"),
240_000);
await goto(new URL(page.url()).pathname);
await caption("Step 2 · Prometheus sees checkout 5xx errors → Alertmanager → a new incident (≈2 min, sped up)");
normal();
await sleep(2500);
await page.locator(`a[href="/incidents/${inc.id}"]`).first().click();
await page.waitForLoadState("networkidle");

// 3. Diagnosis.
await caption("Step 3 · The AI investigates metrics, logs, traces and Kubernetes rollouts…");
fast();
await waitFor("proposal", async () =>
  (await api(`/api/v1/incidents/${inc.id}`)).incident.status === "AWAITING_APPROVAL", 240_000);
await goto(new URL(page.url()).pathname);
normal();
await caption("Step 3 · Diagnosis: bad config rollout in checkout, with every claim citing evidence");
await page.getByText("AI diagnosis").first().scrollIntoViewIfNeeded();
await sleep(5000);
await caption("It proposes ONE fix from a fixed allowlist: roll checkout back. Nothing runs yet.");
await page.getByRole("heading", { name: "Remediation" }).scrollIntoViewIfNeeded();
await sleep(4500);

// 4. Four-eyes approval.
await switchUser("oscar.operator", `/incidents/${inc.id}`, "Step 4 · Oscar (operator) requests approval");
await page.getByRole("heading", { name: "Remediation" }).scrollIntoViewIfNeeded();
await sleep(2000);
await page.getByRole("button", { name: "Request approval" }).click();
await sleep(3000);
await switchUser("anna.approver", `/incidents/${inc.id}`, "Step 5 · A different person, Anna (approver), reviews and approves (four-eyes rule)");
await page.getByRole("heading", { name: "Remediation" }).scrollIntoViewIfNeeded();
await sleep(1500);
await page.getByPlaceholder("Reason (required)").pressSequentially("Config error in logs matches the rollout; rollback is low risk.", { delay: 25 });
await sleep(800);
await page.getByRole("button", { name: "Approve" }).click();
await sleep(3000);

// 5. Execute + verify.
await switchUser("oscar.operator", `/incidents/${inc.id}`, "Step 6 · The policy engine re-checks live state, then the rollback runs");
await page.getByRole("heading", { name: "Remediation" }).scrollIntoViewIfNeeded();
await sleep(1500);
await page.getByRole("button", { name: /^Execute / }).click();
await caption("Step 7 · An independent verifier waits for two healthy checks in a row (sped up)");
fast();
await waitFor("resolved", async () =>
  (await api(`/api/v1/incidents/${inc.id}`)).incident.status === "RESOLVED", 420_000);
await goto(new URL(page.url()).pathname);
normal();
await caption("Resolved ✓  Every step is in a tamper-evident audit trail");
await page.getByRole("heading", { name: "Remediation" }).scrollIntoViewIfNeeded();
await sleep(4000);
await page.getByRole("heading", { name: "Timeline" }).scrollIntoViewIfNeeded();
await sleep(4500);

const video = page.video();
await ctx.close();
await browser.close();
renameSync(await video.path(), `${out}/demo.webm`);
writeFileSync(`${out}/demo-segments.json`, JSON.stringify({ fast: segments }, null, 1));
console.log(`incident ${inc.id}; video ${out}/demo.webm; fast segments`, segments);
