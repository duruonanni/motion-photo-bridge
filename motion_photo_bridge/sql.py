"""Tiny SQL helpers: validate ids, quote literals, never build raw SQL by hand."""
from __future__ import annotations

import re

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
EMAIL_RE = re.compile(r"^[^\s'\"@]{1,64}@[^\s'\"@]{1,255}$")


def uuid(value: str, what: str = "id") -> str:
    value = (value or "").strip()
    if not UUID_RE.match(value):
        raise SystemExit(f"invalid {what}: {value!r} (expected a UUID)")
    return value


def email(value: str) -> str:
    value = (value or "").strip()
    if not EMAIL_RE.match(value):
        raise SystemExit(f"invalid owner email: {value!r}")
    return value


def lit(value: str) -> str:
    """SQL string literal, single quotes doubled."""
    return "'" + str(value).replace("'", "''") + "'"
