"""Conftest for moderation tests.

Stubs out packages that require native libraries or cloud credentials
(psycopg/libpq, minio, PyNaCl, specific Google Cloud modules) so the
test suite runs in CI environments without external dependencies.

IMPORTANT: The top-level `google` namespace package is real (installed via
google-api-python-client). Only stub leaf cloud modules that are absent.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock


def _stub_psycopg() -> None:
    if "psycopg" in sys.modules:
        return
    psycopg = ModuleType("psycopg")
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.transaction = MagicMock(return_value=mock_conn)
    mock_conn.execute = MagicMock()
    psycopg.connect = MagicMock(return_value=mock_conn)  # type: ignore[attr-defined]
    sys.modules["psycopg"] = psycopg


def _stub_leaf(name: str) -> None:
    """Stub a single module without touching its parent packages."""
    if name not in sys.modules:
        sys.modules[name] = MagicMock()


def _stub_cloud_packages() -> None:
    # minio and PyNaCl — not installed in sandbox
    for name in ("minio", "nacl", "nacl.signing", "nacl.encoding", "nacl.exceptions"):
        _stub_leaf(name)

    # google.cloud.vision and google.cloud.pubsub_v1 may not be installed.
    # The top-level `google` and `google.cloud` packages are real namespace
    # packages from google-api-python-client — do NOT overwrite them.
    import importlib
    for leaf in ("google.cloud.vision", "google.cloud.pubsub_v1"):
        try:
            importlib.import_module(leaf)
        except ImportError:
            # Only stub the leaf; parents already exist as namespace packages
            sys.modules[leaf] = MagicMock()


_stub_psycopg()
_stub_cloud_packages()
