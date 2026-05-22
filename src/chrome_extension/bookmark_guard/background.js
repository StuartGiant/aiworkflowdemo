// Bookmark Guard — Manifest V3 service worker (classic script, no ES module imports)

const CORPORATE_DOMAIN = "zeroinsiderai.com";
const STORAGE_KEY = "bookmark_guard_violations";

// Mirrors config/bookmark_guard.yml patterns
const PATTERNS = [
  { name: "pii_endpoint",       pattern: /\/(?:pii|personal[-_]data|user[-_]data|employee[-_]data)(?:\/|$|\?)/i },
  { name: "ssn_in_url",         pattern: /\b\d{3}[-]\d{2}[-]\d{4}\b/i },
  { name: "credit_card_in_url", pattern: /\b(?:\d{4}[-\s]){3}\d{4}\b/i },
  { name: "internal_hr",        pattern: /hr\.(?:internal|corp|company)\./i },
  { name: "payroll_system",     pattern: /(?:payroll|salary|compensation)\.(?:internal|corp)\./i },
  { name: "internal_finance",   pattern: /(?:finance|accounting|treasury)\.(?:internal|corp)\./i },
  { name: "classified_docs",    pattern: /(?:confidential|classified|sensitive|restricted)\.(?:internal|corp)\./i },
  { name: "admin_user_portal",  pattern: /\/(?:admin|superuser|root)\/.*(?:users|employees|personnel)/i },
  { name: "bulk_data_export",   pattern: /\/export(?:\/|$|\?).*\.(?:csv|xlsx|json|parquet)/i },
  { name: "health_records",     pattern: /(?:ehr|emr|hipaa|healthrecords?|medicalrecords?)\.(?:internal|corp)\./i },
  { name: "netflix",            pattern: /\bnetflix\.com(?:[/?:#]|$)/i },
];

function matchingPattern(url) {
  return PATTERNS.find((p) => p.pattern.test(url)) || null;
}

// ------------------------------------------------------------------ entry points

chrome.runtime.onInstalled.addListener(() => {
  console.info("[bookmark_guard] onInstalled — scanning");
  scanAll();
  // Re-scan after 30 s to catch bookmarks restored by Chrome Sync after startup.
  chrome.alarms.create("sync_check", { delayInMinutes: 5 / 60 });
});

chrome.runtime.onStartup.addListener(() => {
  console.info("[bookmark_guard] onStartup — scanning");
  scanAll();
  chrome.alarms.create("sync_check", { delayInMinutes: 5 / 60 });
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "sync_check") {
    console.info("[bookmark_guard] sync_check alarm — re-scanning");
    scanAll();
  }
});

// chrome.bookmarks.onCreated.addListener((_id, node) => {
//   if (node.url) checkAndRemove(node);
// });

// ------------------------------------------------------------------ core logic

async function scanAll() {
  const isCorp = await isCorporateProfile();
  console.info("[bookmark_guard] corporate profile:", isCorp);
  if (!isCorp) return;

  const tree = await chrome.bookmarks.getTree();
  const matches = [];
  collectMatches(tree, matches);
  console.info("[bookmark_guard] matches found:", matches.length);

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
  await removeBookmark({ ...node, patternName: hit.name });
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

async function removeBookmark(node) {
  const hit = matchingPattern(node.url);
  try {
    await chrome.bookmarks.remove(node.id);
    console.info(`[bookmark_guard] removed "${node.title}" (${node.url}) — pattern: ${node.patternName || hit?.name}`);
  } catch (err) {
    console.error(`[bookmark_guard] failed to remove ${node.id}:`, err);
  }
}

async function isCorporateProfile() {
  try {
    const info = await chrome.identity.getProfileUserInfo({ accountStatus: "ANY" });
    if (!info.email) return true;
    return info.email.endsWith(`@${CORPORATE_DOMAIN}`);
  } catch {
    return true;
  }
}

async function persistViolations(nodes) {
  const existing = (await chrome.storage.local.get(STORAGE_KEY))[STORAGE_KEY] || [];
  const newEntries = nodes.map((n) => ({
    id: n.id,
    title: n.title || "",
    url: n.url,
    patternName: n.patternName || matchingPattern(n.url)?.name || "unknown",
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
