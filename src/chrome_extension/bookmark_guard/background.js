import { matchingPattern } from "./patterns.js";

const CORPORATE_DOMAIN = "zeroinsiderai.com";
const STORAGE_KEY = "bookmark_guard_violations";

// ------------------------------------------------------------------ entry points

chrome.runtime.onInstalled.addListener(() => scanAll());
chrome.runtime.onStartup.addListener(() => scanAll());

// Real-time guard: catches bookmarks added while Chrome is running.
chrome.bookmarks.onCreated.addListener((_id, node) => {
  if (node.url) checkAndRemove(node);
});

// ------------------------------------------------------------------ core logic

async function scanAll() {
  if (!await isCorporateProfile()) return;

  const tree = await chrome.bookmarks.getTree();
  const matches = [];
  collectMatches(tree, matches);

  for (const node of matches) {
    await removeBookmark(node);
  }

  if (matches.length > 0) {
    await persistViolations(matches);
    showNotification(matches.length);
  }
}

async function checkAndRemove(node) {
  if (!await isCorporateProfile()) return;

  const hit = matchingPattern(node.url);
  if (!hit) return;

  await removeBookmark(node, hit);
  await persistViolations([{ ...node, patternName: hit.name }]);
  showNotification(1);
}

// ------------------------------------------------------------------ helpers

function collectMatches(nodes, out) {
  for (const node of nodes) {
    if (node.url) {
      const hit = matchingPattern(node.url);
      if (hit) out.push({ ...node, patternName: hit.name });
    }
    if (node.children) collectMatches(node.children, out);
  }
}

async function removeBookmark(node, hit) {
  const pattern = hit ?? matchingPattern(node.url);
  try {
    await chrome.bookmarks.remove(node.id);
    console.info(
      `[bookmark_guard] removed "${node.title}" (${node.url}) — pattern: ${pattern?.name}`
    );
  } catch (err) {
    console.error(`[bookmark_guard] failed to remove ${node.id}:`, err);
  }
}

async function isCorporateProfile() {
  try {
    const info = await chrome.identity.getProfileUserInfo({ accountStatus: "ANY" });
    // Empty email = managed device or dev mode — proceed with scanning.
    // Only skip if we can positively confirm a non-corporate address.
    if (!info.email) return true;
    return info.email.endsWith(`@${CORPORATE_DOMAIN}`);
  } catch {
    return true;
  }
}

async function persistViolations(nodes) {
  const existing = (await chrome.storage.local.get(STORAGE_KEY))[STORAGE_KEY] ?? [];
  const newEntries = nodes.map((n) => ({
    id: n.id,
    title: n.title ?? "",
    url: n.url,
    patternName: n.patternName ?? matchingPattern(n.url)?.name ?? "unknown",
    removedAt: new Date().toISOString(),
  }));
  await chrome.storage.local.set({
    [STORAGE_KEY]: [...existing, ...newEntries].slice(-500),
  });
}

function showNotification(count) {
  chrome.notifications.create({
    type: "basic",
    iconUrl: "icons/icon48.png",
    title: "Security Notice — Bookmark Guard",
    message:
      `${count} bookmark${count > 1 ? "s" : ""} containing sensitive ` +
      `URL${count > 1 ? "s were" : " was"} detected and removed. ` +
      `Please contact the Cybersecurity team if you have questions.`,
    priority: 2,
  });
}
