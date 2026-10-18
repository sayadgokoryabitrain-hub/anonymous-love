from __future__ import annotations

import json
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

LOCALES_DIR = Path(__file__).resolve().parents[1] / "bot" / "locales"
SUPPORTED = ("ru", "hy", "en")
FALLBACK = "ru"

_bundles: dict[str, dict[str, str]] | None = None
_overrides: dict[tuple[str, str], str] = {}
_loaded_at = 0.0


def load_bundles() -> dict[str, dict[str, str]]:
    global _bundles
    if _bundles is None:
        loaded: dict[str, dict[str, str]] = {}
        for code in SUPPORTED:
            loaded[code] = json.loads((LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"))
        _bundles = loaded
    return _bundles


def t(lang: str | None, key: str, **params: object) -> str:
    bundles = load_bundles()
    language = lang if lang in bundles else FALLBACK
    if (language, key) in _overrides:
        raw = _overrides[(language, key)]
    elif key in bundles.get(language, {}):
        raw = bundles[language][key]
    elif key in bundles[FALLBACK]:
        raw = bundles[FALLBACK][key]
    else:
        raw = key
    if not params:
        return raw
    try:
        return raw.format(**params)
    except Exception:
        return raw


def missing_translations() -> dict[str, set[str]]:
    bundles = load_bundles()
    base = set(bundles[FALLBACK])
    return {code: base - set(bundles[code]) for code in SUPPORTED if code != FALLBACK}


async def refresh_overrides(session: AsyncSession, ttl: int = 15) -> None:
    global _loaded_at, _overrides
    if time.time() - _loaded_at < ttl:
        return
    from database.models import LocalizationOverride

    rows = (await session.execute(select(LocalizationOverride))).scalars().all()
    _overrides = {(row.language, row.message_key): row.value for row in rows}
    _loaded_at = time.time()


def clear_override_cache() -> None:
    global _loaded_at, _overrides
    _overrides = {}
    _loaded_at = 0.0
