# Bookmark Guard — Chrome Extension Deployment

## What it does
- Scans all bookmarks on startup (`onInstalled`, `onStartup`) and removes any whose URL matches a sensitive pattern
- Re-scans 5 seconds after startup (`sync_check` alarm) to catch bookmarks restored by Chrome Sync after the initial scan
- The removal goes through Chrome's own bookmark engine (`chrome.bookmarks.remove()`), so the deletion is propagated through Sync to all the user's devices
- Shows a Chrome notification when something is removed
- Stores a local violation log in `chrome.storage.local` (last 500 entries)
- Only acts on corporate profiles (`@zeroinsiderai.com`) — personal profiles are skipped

> **Note:** The `onCreated` real-time listener is intentionally disabled. Enforcement runs at startup only. This is by design — the Python responder (`bookmark_guard`) handles detection and initiates the Chrome reload cycle.

## Permissions required
`bookmarks`, `storage`, `notifications`, `identity`, `alarms`

## How the Python responder uses this extension

The Python `bookmark_guard` automation loads this extension automatically as part of its remediation flow:

1. Detects sensitive bookmarks by reading Chrome profile files
2. Force-closes Chrome if running
3. Removes bookmarks from Chrome's profile files (file-based, atomic)
4. Records violations and preserves evidence
5. Relaunches Chrome with `--load-extension` pointing at this directory
6. Extension fires `onInstalled` → `scanAll()` immediately
7. `sync_check` alarm fires 5 seconds later → `scanAll()` again to catch Sync-restored bookmarks

## Local testing (developer mode)

1. Open Chrome → `chrome://extensions`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked** → select this directory (`src/chrome_extension/bookmark_guard/`)
4. Add a sensitive bookmark (e.g. `https://www.netflix.com/`) — within 5 seconds it should be removed and a notification should appear
5. To inspect extension logs: `chrome://extensions` → Bookmark Guard → Service Worker → Inspect → Console for `[bookmark_guard]` log lines

## Enterprise deployment via Google Workspace Admin Console

1. **Package the extension**
   - Zip the contents of this directory (not the directory itself):
     ```
     cd src/chrome_extension/bookmark_guard
     zip -r bookmark_guard_extension.zip manifest.json background.js icons/
     ```
   - Upload to the Chrome Web Store (private, unlisted) **or** host the `.crx` on an internal server

2. **Force-install via Admin Console**
   - Go to: Admin Console → Devices → Chrome → Apps & Extensions → Users & Browsers
   - Select the target OU
   - Click **+** → Add from Chrome Web Store (or upload CRX)
   - Set installation policy to **Force install**

3. **Verify**
   - On a managed device, open `chrome://extensions` — the extension should appear as managed (cannot be removed by the user)
   - Check Console for `[bookmark_guard] onStartup — scanning` and `[bookmark_guard] sync_check alarm — re-scanning` log lines

## Violation log

Violations are stored locally in `chrome.storage.local` under key `bookmark_guard_violations`.
To inspect from the service worker console:
```js
chrome.storage.local.get("bookmark_guard_violations", console.log)
```

## Keeping patterns in sync

`background.js` contains the `PATTERNS` array which mirrors `config/bookmark_guard.yml`. When patterns change in the YAML, update the `PATTERNS` array in `background.js` and increment the `version` in `manifest.json` to force an update push.
