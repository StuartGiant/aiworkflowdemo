"""Postgres connection helpers.

The writer and reader connections use different DB roles (configured in
PostgresConfig) so the application enforces least privilege at the connection
level — the writer cannot SELECT findings, the reader cannot INSERT custody
events, neither can UPDATE or DELETE anywhere.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg

from .config import PostgresConfig
from .errors import CustodyError


def _dsn(cfg: PostgresConfig, user: str, password: str) -> str:
    return (
        f"host={cfg.host} port={cfg.port} dbname={cfg.database} "
        f"user={user} password={password} "
        f"application_name=evidence-module "
        f"options=-c\\ TimeZone=UTC"
    )


@contextmanager
def writer_conn(cfg: PostgresConfig) -> Iterator[psycopg.Connection]:
    try:
        conn = psycopg.connect(_dsn(cfg, cfg.writer_user, cfg.writer_password))
    except psycopg.Error as exc:
        raise CustodyError("postgres writer connect failed", host=cfg.host) from exc
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def reader_conn(cfg: PostgresConfig) -> Iterator[psycopg.Connection]:
    try:
        conn = psycopg.connect(_dsn(cfg, cfg.reader_user, cfg.reader_password))
    except psycopg.Error as exc:
        raise CustodyError("postgres reader connect failed", host=cfg.host) from exc
    try:
        yield conn
    finally:
        conn.close()
