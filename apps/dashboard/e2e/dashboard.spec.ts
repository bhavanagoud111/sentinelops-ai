import { expect, test, type Page } from "@playwright/test";

async function loginAs(page: Page, username: string) {
  const res = await page.request.post("/api/v1/auth/dev-token", { data: { username } });
  const { access_token } = (await res.json()) as { access_token: string };
  await page.addInitScript((t) => localStorage.setItem("sentinelops.token", t), access_token);
}

test("login screen offers demo identities", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "SentinelOps" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Ada \(admin\)/ })).toBeVisible();
});

test("overview shows KPIs, charts and live stream", async ({ page }) => {
  await loginAs(page, "vera.viewer");
  await page.goto("/");
  await expect(page.getByText("Active incidents").first()).toBeVisible();
  await expect(page.getByRole("img", { name: /Request rate/ })).toBeVisible();
  await expect(page.getByTestId("stream-state")).toHaveText("live", { timeout: 15_000 });
});

test("viewer cannot run experiments; admin can", async ({ page }) => {
  await loginAs(page, "vera.viewer");
  await page.goto("/experiments");
  await expect(page.getByText(/requires the admin role/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Inject" }).first()).toBeDisabled();
});

test("admin sees all five scenarios enabled", async ({ page }) => {
  await loginAs(page, "ada.admin");
  await page.goto("/experiments");
  await expect(page.getByRole("button", { name: "Inject" })).toHaveCount(5);
  await expect(page.getByRole("button", { name: "Inject" }).first()).toBeEnabled();
});

test("incident detail renders lifecycle, diagnosis and timeline", async ({ page }) => {
  await loginAs(page, "vera.viewer");
  await page.goto("/incidents");
  const first = page.locator("tbody tr a").first();
  await first.click();
  await expect(page.getByLabel("Incident lifecycle")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Timeline" })).toBeVisible();
});
