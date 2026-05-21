# Bookmark Guard — Chrome Extension Deployment

## What it does
- Scans all bookmarks on startup and in real-time as new ones are added
- Removes any bookmark whose URL matches a sensitive pattern
- The removal goes through Chrome's own bookmark engine, so Google Sync propagates the deletion to all devices
- Shows a Chrome notification when something is removed
- Stores a local violation log in `chrome.storage.local` (last 500 entries)
- Only acts on corporate profiles (`@zeroinsiderai.com`) — personal profiles are skipped

## Local testing (developer mode)

1. Open Chrome → `chrome://extensions`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked** → select this directory (`src/chrome_extension/bookmark_guard/`)
4. Add a sensitive bookmark (e.g. `https://www.netflix.com/`) — it should be removed within seconds and a notification should appear

## Enterprise deployment via Google Workspace Admin Console

1. **Package the extension**
   - Zip the contents of this directory (not the directory itself):
     ```
     cd src/chrome_extension/bookmark_guard
     zip -r bookmark_guard_extension.zip manifest.json background.js patterns.js icons/
     ```
   - Upload to the Chrome Web Store (private, unlisted) **or** host the `.crx` on an internal server

2. **Force-install via Admin Console**
   - Go to: Admin Console → Devices → Chrome → Apps & Extensions → Users & Browsers
   - Select the target OU
   - Click **+** → Add from Chrome Web Store (or upload CRX)
   - Set installation policy to **Force install**

3. **Verify**
   - On a managed device, open `chrome://extensions` — the extension should appear as managed (cannot be removed by the user)
   - Check `chrome://extensions` → Bookmark Guard → Service Worker → Inspect → Console for `[bookmark_guard]` log lines

## Violation log

Violations are stored locally in `chrome.storage.local` under key `bookmark_guard_violations`.
To inspect from the service worker console:
```js
chrome.storage.local.get("bookmark_guard_violations", console.log)
```

## Keeping patterns in sync

`patterns.js` mirrors `config/bookmark_guard.yml`. When patterns change in the YAML, update
`patterns.js` and increment the `version` in `manifest.json` to force an update push.
