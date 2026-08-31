import { test, expect } from "@playwright/test";
import { goTo, navTab } from "./helpers";

test.describe("Suite 1 — Navigation & Shell", () => {

  test("1.1 header and 4 nav tabs visible", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText("⚗️ DistillPod")).toBeVisible();
    await expect(navTab(page, "Home")).toBeVisible();
    await expect(navTab(page, "Search")).toBeVisible();
    await expect(navTab(page, "Library")).toBeVisible();
    await expect(navTab(page, "Saved")).toBeVisible();
  });

  test("1.2 Home tab active on load", async ({ page }) => {
    await page.goto("/");
    // Active tab has text-indigo-400 class
    await expect(navTab(page, "Home")).toHaveClass(/text-indigo-400/);
    // Other tabs are not active
    await expect(navTab(page, "Search")).not.toHaveClass(/text-indigo-400/);
  });

  test("1.3 tap Search → URL /search, Search active", async ({ page }) => {
    await page.goto("/");
    await goTo(page, "Search");
    await expect(page).toHaveURL("/search");
    await expect(navTab(page, "Search")).toHaveClass(/text-indigo-400/);
  });

  test("1.4 tap Library → URL /library", async ({ page }) => {
    await page.goto("/");
    await goTo(page, "Library");
    await expect(page).toHaveURL("/library");
    await expect(navTab(page, "Library")).toHaveClass(/text-indigo-400/);
  });

  test("1.5 tap Saved → URL /saved", async ({ page }) => {
    await page.goto("/");
    await goTo(page, "Saved");
    await expect(page).toHaveURL("/saved");
  });

  test("1.8 Up Next lives in the header, reachable from any screen", async ({ page }) => {
    await page.goto("/search");
    await page.locator("header").getByRole("button", { name: /Up Next/ }).click();
    await expect(page).toHaveURL("/up-next");
  });

  test("1.9 the Library has sections", async ({ page }) => {
    await page.goto("/library");
    await page.getByRole("tab", { name: /Playlists/ }).click();
    await expect(page).toHaveURL("/library?tab=playlists");
    await page.getByRole("tab", { name: /Storage/ }).click();
    await expect(page.getByText("Audio on disk")).toBeVisible({ timeout: 10_000 });
  });

  test("1.6 tap Home → URL /", async ({ page }) => {
    await page.goto("/search");
    await goTo(page, "Home");
    await expect(page).toHaveURL("/");
  });

  test("1.7 only one tab active at a time", async ({ page }) => {
    await page.goto("/search");
    // Search should be active, others not
    await expect(navTab(page, "Search")).toHaveClass(/text-indigo-400/);
    await expect(navTab(page, "Home")).not.toHaveClass(/text-indigo-400/);
    await expect(navTab(page, "Library")).not.toHaveClass(/text-indigo-400/);
    await expect(navTab(page, "Saved")).not.toHaveClass(/text-indigo-400/);
  });

});
