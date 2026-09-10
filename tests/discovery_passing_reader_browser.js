"use strict";

// Headless local reader proof using the real M2 receipt projection and a
// controlled auth/RPC transport. It never contacts an account or writes state.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

let chromium;
try { ({chromium} = require("playwright")); } catch (_) { process.exit(77); }

const siteRoot = path.resolve(process.argv[2]);
const envelopePath = path.resolve(process.argv[3]);
const artifactRoot = path.resolve(process.argv[4] || path.join(siteRoot, "reader-artifacts"));
const publicBuilt = process.argv[5];
const publicCount = process.argv[6];
const privateBuilt = process.argv[7];
const publicBuiltIso = process.argv[8];
const privateBuiltIso = process.argv[9];
const envelope = JSON.parse(fs.readFileSync(envelopePath, "utf8"));
const expected = Object.fromEntries(
  envelope.edition.entries.map((entry) => [entry.primary_lane, envelope.edition.entries.filter((row) => row.primary_lane === entry.primary_lane)]),
);
const lanes = ["updates", "hot", "interested", "surprise"];

const server = http.createServer((req, res) => {
  const pathname = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
  const file = path.join(siteRoot, pathname === "/" ? "index.html" : pathname);
  if (!file.startsWith(siteRoot + path.sep)) { res.writeHead(404); res.end(); return; }
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript" : "text/html");
    res.end(fs.readFileSync(file));
  } catch (_) { res.writeHead(404); res.end(); }
});

let browser;
async function main() {
  fs.mkdirSync(artifactRoot, {recursive: true});
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    browser = await chromium.launch({headless: true, args: ["--mute-audio"]});
  } catch (error) {
    if (!String(error).includes("Executable doesn't exist")) throw error;
    browser = await chromium.launch({headless: true, channel: "chrome", args: ["--mute-audio"]});
  }
  const context = await browser.newContext({viewport: {width: 1200, height: 850}});
  await context.addInitScript(() => {
    window.speechSynthesis = {cancel() {}, speak() {}, getVoices() { return []; }};
    if (window.HTMLMediaElement) {
      window.HTMLMediaElement.prototype.play = async function play() {};
    }
    if (!navigator.mediaDevices) {
      Object.defineProperty(navigator, "mediaDevices", {value: {getUserMedia: async () => { throw new Error("media disabled"); }}});
    }
  });
  const page = await context.newPage();
  const rpcCalls = [];
  const discoveryAuthHeaders = [];
  await page.route("https://project-ref.supabase.co/rest/v1/rpc/**", async (route) => {
    const request = route.request();
    const authorization = request.headers().authorization;
    const name = new URL(request.url()).pathname.split("/").pop();
    if (name === "discovery_edition") {
      assert.ok(["Bearer controlled-m2-reader-auth", "Bearer other-account-auth"].includes(authorization));
    } else if (name === "set_story_state") {
      assert.equal(authorization, "Bearer controlled-m2-reader-auth");
    } else {
      assert.ok(!authorization || ["Bearer controlled-m2-reader-auth", "Bearer other-account-auth"].includes(authorization));
    }
    rpcCalls.push(name);
    if (name === "discovery_edition") discoveryAuthHeaders.push(authorization);
    let payload;
    if (name === "latest_publication") {
      payload = {finalized_at: envelope.edition.generated_at, initial_history_cursor: null, page_size: 24,
        poll_seconds: 60, publication_seq: 1, topics: [{name: "AI", topic_id: "ai"}]};
    } else if (name === "feed_page" || name === "updates_since") {
      payload = [];
    } else if (name === "discovery_edition") {
      payload = request.headers().authorization.endsWith("other-account-auth")
        ? {schema_version: 1, status: "unavailable", reason_code: "no_private_edition", edition: null}
        : envelope;
    } else if (name === "set_story_state") {
      const body = request.postDataJSON();
      payload = {status: "updated", read_at: body.p_read ? envelope.edition.generated_at : null,
        saved_at: body.p_saved ? envelope.edition.generated_at : null, revision: 1};
    } else {
      throw new Error(`Unexpected RPC ${name}`);
    }
    await route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify(payload)});
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  const headerCount = () => page.locator(".edition-meta span").filter({hasText: / stories?$/});
  const publicStaleState = await page.locator("#stale").evaluate((node) => ({
    hidden: node.hidden,
    previousHidden: node.previousElementSibling.hidden,
  }));
  await page.waitForFunction(() => document.querySelector("button[data-discovery-lane='updates']")?.getAttribute("aria-pressed") === "true");
  assert.equal(await headerCount().textContent(), `${envelope.edition.entries.length} stories`);
  assert.equal(await page.locator(".edition-meta span").first().textContent(), privateBuilt);
  assert.notEqual(privateBuilt, publicBuilt);
  assert.match(await page.locator(".edition-meta span").first().textContent(), /UTC/);
  assert.equal(await page.locator("#stale").getAttribute("data-built"), privateBuiltIso);
  assert.equal(await page.locator("#stale").isHidden(), true);
  assert.equal(await page.locator("#stale").evaluate((node) => node.previousElementSibling.hidden), true);

  for (const lane of lanes) {
    await page.locator(`button[data-discovery-lane="${lane}"]`).click();
    await page.waitForFunction((selected) => document.querySelector(".discovery-section")?.dataset.discoverySelectedLane === selected, lane);
    const cards = page.locator(`.discovery-section .card[data-discovery-lane="${lane}"]:visible`);
    assert.equal(await cards.count(), expected[lane].length, `${lane} visible count`);
    const ids = await cards.evaluateAll((nodes) => nodes.map((node) => node.dataset.storyId));
    assert.equal(new Set(ids).size, ids.length, `${lane} unique story IDs`);
    for (const entry of expected[lane]) {
      const card = page.locator(`.discovery-section .card[data-story-id="${entry.card.story_id}"]`);
      assert.equal(await card.locator(".headline").textContent(), entry.card.title);
      assert.match(await card.locator(".signal").textContent(), new RegExp(entry.reason.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
      assert.equal((await card.getAttribute("data-topic-api-ids")) || "", [...entry.card.topic_ids].sort().join(" "));
    }
  }

  const expanded = page.locator(".discovery-section .card:visible").first();
  await expanded.locator(".accordion-toggle").click();
  await expanded.locator(".detail").waitFor({state: "visible"});
  assert.equal(await expanded.locator(".detail").isVisible(), true);
  assert.equal(await expanded.locator(".read-action").isVisible(), true);
  assert.equal(await expanded.locator(".save-action").isVisible(), true);
  assert.match(await expanded.locator(".signal").textContent(), /Why this story/);
  await page.screenshot({path: path.join(artifactRoot, "discovery-passing-desktop.png"), fullPage: true});
  await page.setViewportSize({width: 390, height: 844});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  await page.screenshot({path: path.join(artifactRoot, "discovery-passing-mobile.png"), fullPage: true});
  await page.locator("button[data-discovery-public]").click();
  await page.waitForFunction(() => !document.querySelector(".discovery-section"));
  assert.equal(await page.locator(".edition-meta span").first().textContent(), publicBuilt);
  assert.equal(await headerCount().textContent(), publicCount);
  assert.equal(await page.locator("#stale").getAttribute("data-built"), publicBuiltIso);
  await page.evaluate(() => {
    window.__account = "other";
    window.dispatchEvent(new Event("news-curator:auth-changed"));
  });
  await page.waitForFunction(
    () => document.querySelector("#discovery-status")?.textContent === "Your private discovery edition is not ready yet. Public stories are available.",
    null,
    {timeout: 5000},
  );
  assert.equal(discoveryAuthHeaders.at(-1), "Bearer other-account-auth");
  assert.equal(await page.locator("#discovery-controls").isHidden(), false);
  assert.equal(await page.locator(".discovery-section").count(), 0);
  assert.equal(await page.locator(".edition-meta span").first().textContent(), publicBuilt);
  assert.equal(await headerCount().textContent(), publicCount);
  assert.equal(await page.locator("#stale").getAttribute("data-built"), publicBuiltIso);
  assert.equal(await page.locator("#stale").isHidden(), publicStaleState.hidden);
  assert.equal(await page.locator("#stale").evaluate((node) => node.previousElementSibling.hidden), publicStaleState.previousHidden);
  await page.evaluate(() => {
    window.__account = "owner";
    window.dispatchEvent(new Event("news-curator:auth-changed"));
  });
  await page.waitForFunction(() => document.querySelector("button[data-discovery-lane='updates']")?.getAttribute("aria-pressed") === "true");
  assert.equal(await headerCount().textContent(), `${envelope.edition.entries.length} stories`);
  assert.equal(await page.locator(".edition-meta span").first().textContent(), privateBuilt);
  assert.equal(await page.locator("#stale").getAttribute("data-built"), privateBuiltIso);
  assert.equal(await page.locator("#stale").isHidden(), true);
  assert.equal(await page.locator("#stale").evaluate((node) => node.previousElementSibling.hidden), true);
  await page.locator("button[data-discovery-lane='updates']").click();
  await page.waitForFunction(() => document.querySelector(".discovery-section"));
  assert.equal(await headerCount().textContent(), `${envelope.edition.entries.length} stories`);
  await page.evaluate(() => { window.__signed = false; window.dispatchEvent(new Event("news-curator:auth-changed")); });
  await page.waitForFunction(() => document.querySelector("#discovery-controls").hidden);
  await page.waitForFunction(() => !document.querySelector(".discovery-section"));
  assert.equal(await page.locator(".edition-meta span").first().textContent(), publicBuilt);
  assert.equal(await headerCount().textContent(), publicCount);
  assert.equal(await page.locator("#stale").getAttribute("data-built"), publicBuiltIso);
  assert.equal(await page.locator("#stale").isHidden(), publicStaleState.hidden);
  assert.equal(await page.locator("#stale").evaluate((node) => node.previousElementSibling.hidden), publicStaleState.previousHidden);
  fs.writeFileSync(path.join(artifactRoot, "reader-regression-report.json"), JSON.stringify({
    lanes: Object.fromEntries(lanes.map((lane) => [lane, expected[lane].length])),
    total: envelope.edition.entries.length,
    rpc_calls: rpcCalls,
    transport: "controlled auth and RPC interception; no account mutation",
    screenshots: ["discovery-passing-desktop.png", "discovery-passing-mobile.png"],
  }, null, 2) + "\n");
  await context.close();
  console.log("discovery passing reader: PASS (controlled auth transport, desktop/mobile screenshots)");
}

main().catch((error) => { console.error(error); process.exitCode = 1; })
  .finally(async () => { if (browser) await browser.close(); await new Promise((resolve) => server.close(resolve)); });
setTimeout(() => process.exit(1), 45000).unref();
