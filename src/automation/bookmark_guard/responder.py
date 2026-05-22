"""responder.py — removes sensitive bookmarks/homepages and records violations.

File modifications are atomic: each write goes to a sibling .tmp file then
os.replace() swaps it in, so a crash mid-write leaves the original intact.

Chrome's bookmark checksum is cleared on write; Chrome silently recalculates
it on next launch.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import BookmarkGuardConfig, SensitivePattern
from .models import BookmarkMatch, RemovalOutcome, ScanResult

logger = logging.getLogger(__name__)

_CHROME_DATA_DIR = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
_PROFILE_ROOT_KEYS = ("bookmark_bar", "other", "synced")


class BookmarkResponder:
    def __init__(self, config: BookmarkGuardConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------ public

    def respond(self, result: ScanResult) -> list[RemovalOutcome]:
        """Remove all sensitive bookmarks/homepages and write violation records.

        For each affected Chrome profile, the Bookmarks file is preserved as a
        tamper-evident artefact in MinIO (via the evidence module) *before* any
        modification.  The artefact_id is stored on each RemovalOutcome and
        written to bookmark_violations.evidence_artefact_id.
        """
        chrome_was_running = _is_chrome_running()
        self._check_chrome(result)

        # Group matches by profile so we read/write each file once.
        by_profile: dict[str, list[BookmarkMatch]] = {}
        for match in result.matches:
            by_profile.setdefault(match.profile_dir, []).append(match)

        # Resolve evidence config and case once for the whole run.
        evidence_cfg, case_id = None, None
        if not self._config.dry_run:
            evidence_cfg = _try_load_evidence_config()
            if evidence_cfg:
                case_id = _get_or_create_case(
                    self._config.postgres, result.hostname, result.os_username
                )

        outcomes: list[RemovalOutcome] = []

        for profile_name, matches in by_profile.items():
            profile_dir = _CHROME_DATA_DIR / profile_name

            bm_matches = [m for m in matches if m.item_type == "bookmark"]
            hp_matches = [m for m in matches if m.item_type == "homepage"]

            # Preserve the Bookmarks file snapshot before any modification.
            artefact_id: str | None = None
            if bm_matches and evidence_cfg and case_id:
                artefact_id = _preserve_bookmarks_evidence(
                    evidence_cfg, case_id,
                    profile_dir, profile_name,
                    result.hostname, result.os_username,
                )

            if bm_matches:
                bm_outcomes = self._remove_bookmarks(profile_dir, bm_matches)
                # Attach the artefact_id to every bookmark outcome in this profile.
                outcomes.extend(
                    RemovalOutcome(
                        match=o.match,
                        action_taken=o.action_taken,
                        action_error=o.action_error,
                        evidence_artefact_id=artefact_id,
                    )
                    for o in bm_outcomes
                )

            if hp_matches:
                hp_outcomes = self._remove_homepages(profile_dir, hp_matches)
                outcomes.extend(hp_outcomes)

        if not self._config.dry_run:
            self._record_violations(result, outcomes)

        if chrome_was_running and not self._config.dry_run:
            ext_cfg = self._config.extension
            if ext_cfg is not None:
                user_data_dir = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
                subprocess.Popen([
                    "open", "-a", "Google Chrome",
                    "--args",
                    f"--user-data-dir={user_data_dir}",
                    f"--profile-directory={ext_cfg.chrome_profile}",
                    f"--load-extension={ext_cfg.extension_path}",
                    "--no-first-run",
                ])
            else:
                subprocess.Popen(["open", "-a", "Google Chrome"])
            logger.info("bookmark_guard.responder.chrome_restarted")

        return outcomes

    # -------------------------------------------------------- chrome check

    def _check_chrome(self, result: ScanResult) -> None:
        if not _is_chrome_running():
            return

        logger.warning(
            "bookmark_guard.responder.chrome_running.force_closing",
            extra={"hostname": result.hostname},
        )

        # Graceful quit first; hard-kill if Chrome is still up after 5 s.
        subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to quit'],
            capture_output=True,
        )
        for _ in range(5):
            time.sleep(1)
            if not _is_chrome_running():
                break
        else:
            subprocess.run(["pkill", "-9", "Google Chrome"], capture_output=True)
            time.sleep(1)

        logger.info("bookmark_guard.responder.chrome_closed")

    # -------------------------------------------------- bookmark removal

    def _remove_bookmarks(
        self, profile_dir: Path, matches: list[BookmarkMatch]
    ) -> list[RemovalOutcome]:
        bm_file = profile_dir / "Bookmarks"
        if not bm_file.exists():
            return [
                RemovalOutcome(match=m, action_taken="failed", action_error="Bookmarks file not found")
                for m in matches
            ]

        if self._config.dry_run:
            logger.info(
                "bookmark_guard.responder.dry_run.bookmarks",
                extra={"profile": profile_dir.name, "count": len(matches)},
            )
            return [RemovalOutcome(match=m, action_taken="skipped") for m in matches]

        try:
            with bm_file.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            err = str(exc)
            return [RemovalOutcome(match=m, action_taken="failed", action_error=err) for m in matches]

        urls_to_remove = {m.url for m in matches}
        removed_urls: set[str] = set()

        roots = data.get("roots", {})
        for root_key in _PROFILE_ROOT_KEYS:
            if isinstance(roots.get(root_key), dict):
                roots[root_key], found = _filter_node(roots[root_key], urls_to_remove, self._config.patterns)
                removed_urls.update(found)

        # Clear checksum; Chrome recalculates silently on next launch.
        data["checksum"] = ""

        try:
            _atomic_write_json(bm_file, data)
        except OSError as exc:
            err = str(exc)
            return [RemovalOutcome(match=m, action_taken="failed", action_error=err) for m in matches]

        logger.info(
            "bookmark_guard.responder.bookmarks_removed",
            extra={"profile": profile_dir.name, "removed": len(removed_urls)},
        )

        return [
            RemovalOutcome(
                match=m,
                action_taken="removed" if m.url in removed_urls else "failed",
                action_error=None if m.url in removed_urls else "URL not found in file during removal",
            )
            for m in matches
        ]

    # -------------------------------------------------- homepage removal

    def _remove_homepages(
        self, profile_dir: Path, matches: list[BookmarkMatch]
    ) -> list[RemovalOutcome]:
        prefs_file = profile_dir / "Preferences"
        if not prefs_file.exists():
            return [
                RemovalOutcome(match=m, action_taken="failed", action_error="Preferences file not found")
                for m in matches
            ]

        if self._config.dry_run:
            logger.info(
                "bookmark_guard.responder.dry_run.homepage",
                extra={"profile": profile_dir.name},
            )
            return [RemovalOutcome(match=m, action_taken="skipped") for m in matches]

        try:
            with prefs_file.open(encoding="utf-8") as fh:
                prefs = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            err = str(exc)
            return [RemovalOutcome(match=m, action_taken="failed", action_error=err) for m in matches]

        urls_to_remove = {m.url for m in matches}
        current_homepage = prefs.get("homepage", "")

        if current_homepage not in urls_to_remove:
            return [
                RemovalOutcome(match=m, action_taken="failed", action_error="Homepage URL changed before removal")
                for m in matches
            ]

        prefs["homepage"] = ""
        prefs["homepage_is_newtabpage"] = True

        try:
            _atomic_write_json(prefs_file, prefs)
        except OSError as exc:
            err = str(exc)
            return [RemovalOutcome(match=m, action_taken="failed", action_error=err) for m in matches]

        logger.info(
            "bookmark_guard.responder.homepage_removed",
            extra={"profile": profile_dir.name, "url": current_homepage},
        )

        return [RemovalOutcome(match=m, action_taken="removed") for m in matches]

    # -------------------------------------------------- database record

    def _record_violations(
        self, result: ScanResult, outcomes: list[RemovalOutcome]
    ) -> None:
        if not outcomes:
            return

        # Lazy import — psycopg and libpq are only required in live mode.
        try:
            import psycopg  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "psycopg / libpq not available; cannot write violations to database. "
                "Install libpq (e.g. brew install libpq) and re-run."
            ) from exc

        cfg = self._config.postgres
        dsn = (
            f"host={cfg.host} port={cfg.port} dbname={cfg.database} "
            f"user={cfg.writer_user} password={cfg.writer_password} "
            f"options=-c\\ TimeZone=UTC"
        )

        rows = [
            (
                uuid.uuid4(),
                result.hostname,
                result.os_username,
                o.match.profile_dir,
                o.match.chrome_email,
                o.match.url,
                o.match.title,
                o.match.item_type,
                o.match.pattern_name,
                o.action_taken,
                o.action_error,
                o.evidence_artefact_id,
            )
            for o in outcomes
        ]

        try:
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO bookmark_violations (
                            violation_id, hostname, os_username,
                            chrome_profile, chrome_email,
                            url, title, item_type, pattern_name,
                            action_taken, action_error, evidence_artefact_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        rows,
                    )
                conn.commit()
        except psycopg.Error as exc:
            logger.error(
                "bookmark_guard.responder.db_write_failed",
                extra={"error": str(exc)},
            )
            raise


# --------------------------------------------------------------- helpers

def _is_chrome_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Google Chrome"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _filter_node(
    node: dict,
    urls_to_remove: set[str],
    patterns: tuple[SensitivePattern, ...],
) -> tuple[dict, set[str]]:
    """Return (modified_node, set_of_removed_urls).

    Removes all URL bookmark nodes whose URL is in urls_to_remove OR matches
    any sensitive pattern (re-scan guarantees nothing slips through on re-run).
    """
    removed: set[str] = set()

    if node.get("type") == "folder":
        new_children: list[dict] = []
        for child in node.get("children", []):
            if not isinstance(child, dict):
                continue
            if child.get("type") == "url":
                url = child.get("url", "")
                if url in urls_to_remove or _matches_any(url, patterns):
                    removed.add(url)
                    continue
            child, child_removed = _filter_node(child, urls_to_remove, patterns)
            removed.update(child_removed)
            new_children.append(child)
        node = dict(node)
        node["children"] = new_children

    return node, removed


def _matches_any(url: str, patterns: tuple[SensitivePattern, ...]) -> bool:
    return any(p.pattern.search(url) for p in patterns)


def _atomic_write_json(path: Path, data: dict) -> None:
    parent = path.parent
    fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=3)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --------------------------------------------------------------- evidence helpers

def _try_load_evidence_config():
    """Load EvidenceConfig from env; return None and log a warning on failure."""
    try:
        from evidence.config import EvidenceConfig  # noqa: PLC0415
        return EvidenceConfig.from_env()
    except Exception as exc:
        logger.warning(
            "bookmark_guard.responder.evidence_config_unavailable",
            extra={"error": str(exc)},
        )
        return None


def _get_or_create_case(postgres_cfg, hostname: str, os_username: str) -> str | None:
    """Return a case_id for today's bookmark-guard run on this host.

    Uses case_code BG-<hostname>-<YYYY-MM-DD>.  Creates the case if it doesn't
    exist.  Returns None and logs on failure so the caller can proceed without
    evidence preservation.
    """
    from datetime import date  # noqa: PLC0415

    try:
        import psycopg  # noqa: PLC0415
    except ImportError:
        return None

    case_code = f"BG-{hostname}-{date.today().isoformat()}"
    dsn = (
        f"host={postgres_cfg.host} port={postgres_cfg.port} "
        f"dbname={postgres_cfg.database} "
        f"user={postgres_cfg.writer_user} password={postgres_cfg.writer_password} "
        f"options=-c\\ TimeZone=UTC"
    )

    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT case_id FROM cases WHERE case_code = %s", (case_code,)
                )
                row = cur.fetchone()
            if row:
                return str(row[0])

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cases (case_code, title, subject_ref, severity)
                    VALUES (%s, %s, %s, 'low') RETURNING case_id
                    """,
                    (
                        case_code,
                        f"Bookmark Guard: sensitive URLs detected on {hostname}",
                        os_username,
                    ),
                )
                case_id = str(cur.fetchone()[0])
            conn.commit()
            logger.info(
                "bookmark_guard.responder.case_created",
                extra={"case_code": case_code, "case_id": case_id},
            )
            return case_id
    except Exception as exc:
        logger.warning(
            "bookmark_guard.responder.case_lookup_failed",
            extra={"error": str(exc)},
        )
        return None


def _preserve_bookmarks_evidence(
    evidence_cfg,
    case_id: str,
    profile_dir: Path,
    profile_name: str,
    hostname: str,
    os_username: str,
) -> str | None:
    """Snapshot the Chrome Bookmarks file into the evidence vault.

    Returns the artefact_id on success, None on failure (non-fatal).
    """
    bm_file = profile_dir / "Bookmarks"
    if not bm_file.exists():
        return None

    try:
        from evidence import record_evidence  # noqa: PLC0415
        data = bm_file.read_bytes()
        result = record_evidence(
            evidence_cfg,
            case_id=case_id,
            source_system="bookmark_guard.chrome",
            collection_method="manual",
            query=(
                f"Chrome Bookmarks file snapshot before sensitive-URL removal "
                f"— profile={profile_name} host={hostname}"
            ),
            collector_principal=f"bookmark_guard:responder:{os_username}",
            original_tz="+00:00",
            data=data,
            mime_type="application/json",
            pii_tags=["browser_bookmark", "pii_url"],
            host=hostname,
        )
        logger.info(
            "bookmark_guard.responder.evidence_preserved",
            extra={
                "artefact_id": result.artefact_id,
                "profile": profile_name,
                "bytes": len(data),
                "sha256": result.manifest.sha256,
            },
        )
        return result.artefact_id
    except Exception as exc:
        logger.warning(
            "bookmark_guard.responder.evidence_preservation_failed",
            extra={"profile": profile_name, "error": str(exc)},
        )
        return None
