"""Ed25519 signing for evidence manifests and custody rows.

Demo-grade: keys live on disk, read via the paths in ``EvidenceConfig.signing``.
Production swap-in is a Cloud KMS / HSM-backed asymmetric key; the public
``sign`` / ``verify`` interface stays the same.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from .config import SigningConfig
from .errors import SignatureError


def load_signing_key(path: Path) -> SigningKey:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SignatureError("cannot read signing key", path=str(path)) from exc
    seed = _extract_ed25519_bytes(raw, path)
    return SigningKey(seed)


def load_verify_key(path: Path) -> VerifyKey:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SignatureError("cannot read verify key", path=str(path)) from exc
    key_bytes = _extract_ed25519_bytes(raw, path)
    return VerifyKey(key_bytes)


def _extract_ed25519_bytes(raw: bytes, path: Path) -> bytes:
    """Return the 32-byte Ed25519 seed/key from either raw bytes or PEM/DER.

    Accepts:
      - 32 raw bytes (already unwrapped)
      - PEM-encoded PKCS#8 private key  (openssl genpkey output)
      - PEM-encoded SPKI public key     (openssl pkey -pubout output)
      - DER-encoded PKCS#8 or SPKI

    For both PKCS#8 and SPKI Ed25519, the 32-byte payload is always the
    last 32 bytes of the DER structure.
    """
    if len(raw) == 32:
        return raw

    # Decode PEM to DER if necessary.
    der = raw
    text = raw.decode("ascii", errors="ignore")
    if "-----BEGIN" in text:
        import base64  # noqa: PLC0415
        b64_lines = [
            line for line in text.splitlines()
            if line and not line.startswith("-----")
        ]
        try:
            der = base64.b64decode("".join(b64_lines))
        except Exception as exc:
            raise SignatureError(
                "failed to decode PEM key", path=str(path)
            ) from exc

    # For Ed25519, the 32-byte seed/key is the last 32 bytes of the DER wrapper.
    if len(der) < 32:
        raise SignatureError(
            "key DER too short", path=str(path), length=len(der)
        )
    return der[-32:]


def fingerprint(verify_key: VerifyKey) -> str:
    """Stable, short identifier for a public key.

    Format: ``ed25519:<first-16-hex-chars-of-sha256(pubkey)>``.
    """
    digest = hashlib.sha256(bytes(verify_key)).hexdigest()
    return f"ed25519:{digest[:16]}"


def sign(signing_key: SigningKey, data: bytes) -> str:
    """Return a base64-encoded detached signature over ``data``."""
    sig = signing_key.sign(data).signature
    return base64.b64encode(sig).decode("ascii")


def verify(verify_key: VerifyKey, data: bytes, signature_b64: str) -> None:
    """Raise ``SignatureError`` if the signature does not match."""
    try:
        sig = base64.b64decode(signature_b64.encode("ascii"), validate=True)
    except ValueError as exc:
        raise SignatureError("signature is not valid base64") from exc
    try:
        verify_key.verify(data, sig)
    except BadSignatureError as exc:
        raise SignatureError("signature does not match data") from exc


def generate_keypair(out_dir: Path, key_id_hint: str | None = None) -> tuple[Path, Path, str]:
    """Convenience for bootstrap / tests. Creates ``signing.key`` and
    ``signing.pub`` under ``out_dir`` (mode 0600 / 0644). Returns the two paths
    plus the public-key fingerprint.

    NEVER call this in production with the on-disk paths configured for
    evidence signing — production keys must be HSM-resident.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    sk = SigningKey.generate()
    vk = sk.verify_key
    priv_path = out_dir / "signing.key"
    pub_path = out_dir / "signing.pub"
    priv_path.write_bytes(bytes(sk))
    pub_path.write_bytes(bytes(vk))
    try:
        priv_path.chmod(0o600)
        pub_path.chmod(0o644)
    except OSError:
        # On some platforms (e.g. Windows) chmod is a best-effort.
        pass
    fp = fingerprint(vk)
    # key_id_hint is recorded by the caller in config; we just return the fp.
    _ = key_id_hint
    return priv_path, pub_path, fp


def signing_config_keys(cfg: SigningConfig) -> tuple[SigningKey, VerifyKey]:
    """Helper: load both keys from a SigningConfig, raising one consolidated
    SignatureError on failure."""
    sk = load_signing_key(cfg.private_key_path)
    vk = load_verify_key(cfg.public_key_path)
    if fingerprint(vk) != cfg.key_id:
        raise SignatureError(
            "configured SIGNING_KEY_ID does not match the public key fingerprint",
            configured=cfg.key_id,
            actual=fingerprint(vk),
        )
    return sk, vk
