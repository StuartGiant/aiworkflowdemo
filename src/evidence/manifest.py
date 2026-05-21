"""Evidence manifest: schema, canonicalisation, hashing.

The manifest is the cryptographic binding between an artefact's raw bytes and
its collection metadata. Once signed it becomes the canonical record of what
was collected, by whom, when, from where, and with which query.

Canonical serialisation is ``json.dumps(sort_keys=True, separators=(",", ":"),
ensure_ascii=False)``. This is deterministic enough for the demo (no floats are
emitted by this module) but is NOT full RFC 8785 JCS. Production should swap
in a JCS implementation; see docs/adr/0002-evidence-store.md.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import ValidationError

MANIFEST_VERSION = 1
SIGNATURE_FIELDS = ("signature_alg", "signature", "signing_key_id")

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SOURCE_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+){1,3}$")


@dataclass(slots=True)
class Manifest:
    artefact_id: str
    case_id: str
    source_system: str
    collection_method: str
    query: str
    collector_principal: str
    collected_at_utc: str
    original_tz: str
    bytes: int
    sha256: str
    mime_type: str
    s3_uri: str
    pii_tags: list[str]
    retention_class: str
    signing_key_id: str
    signature_alg: str = "ed25519"
    signature: str = ""           # filled in by signing.sign_manifest
    ecs_index: str | None = None
    ecs_doc_id: str | None = None
    manifest_version: int = MANIFEST_VERSION
    # Free-form additional context. Never carries the artefact bytes.
    extra: dict[str, Any] = field(default_factory=dict)

    # -- validation -------------------------------------------------------

    def validate(self) -> None:
        """Validate every field. Raises ValidationError on the first problem.

        Assume hostile input — every field is checked even though most are
        produced by record_evidence itself.
        """
        if not _UUID_RE.match(self.artefact_id):
            raise ValidationError("artefact_id is not a UUID", value=self.artefact_id)
        if not _UUID_RE.match(self.case_id):
            raise ValidationError("case_id is not a UUID", value=self.case_id)
        if not _SOURCE_RE.match(self.source_system):
            raise ValidationError("source_system has unexpected shape", value=self.source_system)
        if self.collection_method not in {"api", "export", "kql", "spl", "manual"}:
            raise ValidationError("collection_method invalid", value=self.collection_method)
        if not self.query or len(self.query) > 16_384:
            raise ValidationError("query empty or too long", length=len(self.query))
        if not self.collector_principal:
            raise ValidationError("collector_principal empty")
        try:
            ts = datetime.fromisoformat(self.collected_at_utc.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("collected_at_utc not ISO-8601", value=self.collected_at_utc) from exc
        if ts.tzinfo is None or ts.utcoffset() != timezone.utc.utcoffset(ts):
            raise ValidationError("collected_at_utc not UTC", value=self.collected_at_utc)
        if not self.original_tz:
            raise ValidationError("original_tz empty")
        if self.bytes < 0:
            raise ValidationError("bytes negative", value=self.bytes)
        if len(self.sha256) != 64 or not all(c in "0123456789abcdef" for c in self.sha256):
            raise ValidationError("sha256 not 64-hex-char lowercase", value=self.sha256)
        if not self.mime_type:
            raise ValidationError("mime_type empty")
        if not self.s3_uri.startswith("s3://"):
            raise ValidationError("s3_uri not s3://", value=self.s3_uri)
        if not self.retention_class:
            raise ValidationError("retention_class empty")
        if self.signature_alg != "ed25519":
            raise ValidationError("only ed25519 supported in this build", value=self.signature_alg)
        if not self.signing_key_id:
            raise ValidationError("signing_key_id empty")
        if self.manifest_version != MANIFEST_VERSION:
            raise ValidationError(
                "manifest_version mismatch",
                got=self.manifest_version,
                want=MANIFEST_VERSION,
            )

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop optional Nones so the canonical form is stable when callers
        # omit ecs_index / ecs_doc_id.
        return {k: v for k, v in d.items() if v is not None}

    def to_json_bytes(self) -> bytes:
        """Pretty JSON for storage. Not used for signing."""
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8")

    def canonical_bytes_for_signing(self) -> bytes:
        """Canonical bytes used to compute / verify the signature.

        Excludes the signature itself so the signature can be embedded in the
        same document without forming a cycle.
        """
        d = self.to_dict()
        for field_name in SIGNATURE_FIELDS:
            d.pop(field_name, None)
        return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        # Allow callers to pass dicts loaded from JSON. Unknown keys go into
        # extra so we don't crash on forward-compatible documents.
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        extra = {k: v for k, v in data.items() if k not in known}
        if extra:
            kwargs["extra"] = extra
        return cls(**kwargs)


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def utc_now_iso() -> str:
    """Current time, UTC, ISO-8601 with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
