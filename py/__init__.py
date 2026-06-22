"""
* LocalJSON Plugin
* ----------------
* Lets Proxyshop pull card data from a local JSON file instead of (or before)
* the Scryfall API.
*
* How it works
*   Proxyshop fetches every card's data through a single function,
*   `src.cards.get_card_data(card, cfg, logger)`, which returns a Scryfall-shaped
*   dict that the layout/template code then consumes. This plugin wraps that
*   function: when local-JSON mode is on and a matching card is found in your
*   data file, the local entry is served; otherwise behaviour depends on `mode`
*   (see settings.json). No core Proxyshop files are modified.
*
* The option (settings.json, next to this plugin's manifest)
*   enabled       true/false  -> master switch for the plugin
*   mode          "merge"     -> always query Scryfall, then overlay the
*                                non-null / non-empty fields from the JSON entry
*                                on top (per-face for double-faced cards)
*                 "auto"      -> use JSON when the card is present, else Scryfall
*                 "strict"    -> use JSON only; never call Scryfall
*                 "off"       -> ignore JSON; behave like vanilla Proxyshop
*   data_file     path to the JSON data file (absolute, or relative to this
*                 plugin folder, or relative to the current working directory)
*   match_set     true  -> when several entries share a name, prefer the one
*                 whose set matches the [SET] tag in the art filename
*   match_number  true  -> likewise prefer the entry matching the {num} tag
*   log           true  -> print a short line to the Proxyshop console on hits
*
* Both settings.json and the data file are re-read automatically when their
* mod/time changes, so you can edit cards between renders without restarting.
"""

# Standard Library Imports
import os
import re
import sys
import json
from pathlib import Path
from typing import Any, Optional

# Root of this plugin (……/plugins/LocalJSON)
PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# Optional niceties from Proxyshop; safe fallbacks if unavailable (e.g. in tests)
try:
    from omnitils.strings import normalize_str as _omni_normalize
except Exception:  # pragma: no cover - exercised only outside Proxyshop
    _omni_normalize = None

try:
    from src.console import msg_warn, msg_success
except Exception:  # pragma: no cover
    def msg_warn(s: str) -> str:
        return s

    def msg_success(s: str) -> str:
        return s


# Holds the function we replace, so we can fall back to it.
_ORIGINAL_GET_CARD_DATA = None

# Simple mtime-aware caches so edits are picked up without a restart.
_SETTINGS_CACHE: dict = {"path": None, "mtime": None, "value": None}
_DATA_CACHE: dict = {"path": None, "mtime": None, "index": None}

# Mana symbol -> colour, for deriving colour identity from a mana cost string.
_COLOR_LETTERS = ("W", "U", "B", "R", "G")

# Friendly field aliases -> canonical Scryfall keys.
_ALIASES = {
    "type": "type_line",
    "types": "type_line",
    "text": "oracle_text",
    "rules": "oracle_text",
    "rules_text": "oracle_text",
    "flavor": "flavor_text",
    "flavour": "flavor_text",
    "flavour_text": "flavor_text",
    "cost": "mana_cost",
    "mana": "mana_cost",
    "manacost": "mana_cost",
    "pt_power": "power",
    "pt_toughness": "toughness",
    "number": "collector_number",
    "set_code": "set",
    "faces": "card_faces",
}


"""
* String / colour helpers
"""


def _normalize(name: str) -> str:
    """Normalise a card name for matching. Mirrors Proxyshop's normalize_str
    (case-folded, accent-stripped, whitespace removed) when available."""
    if not name:
        return ""
    if _omni_normalize is not None:
        try:
            return _omni_normalize(name, no_space=True)
        except TypeError:
            return _omni_normalize(name)
    # Fallback: lowercase, drop accents and any non-alphanumeric character.
    import unicodedata
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _derive_color_identity(*sources: Any) -> list[str]:
    """Collect W/U/B/R/G letters appearing in any of the given mana strings."""
    found: list[str] = []
    for src in sources:
        if not src:
            continue
        text = src if isinstance(src, str) else " ".join(map(str, src))
        for letter in _COLOR_LETTERS:
            if letter in text and letter not in found:
                found.append(letter)
    # Keep canonical WUBRG order.
    return [c for c in _COLOR_LETTERS if c in found]


"""
* Settings + data loading (mtime-cached)
"""


def _load_settings() -> dict:
    """Load settings.json from the plugin root, re-reading on change."""
    defaults = {
        "enabled": True,
        "mode": "auto",
        "data_file": "cards.json",
        "match_set": True,
        "match_number": True,
        "log": True,
    }
    path = PLUGIN_ROOT / "settings.json"
    if not path.is_file():
        return defaults
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return defaults
    if _SETTINGS_CACHE["value"] is not None and _SETTINGS_CACHE["mtime"] == mtime:
        return _SETTINGS_CACHE["value"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        defaults.update({k: v for k, v in loaded.items() if v is not None})
    except Exception:
        pass
    _SETTINGS_CACHE.update({"path": path, "mtime": mtime, "value": defaults})
    return defaults


def _resolve_data_path(data_file: str) -> Optional[Path]:
    """Resolve the data file path: absolute, then relative to the plugin
    folder, then relative to the current working directory."""
    if not data_file:
        return None
    candidate = Path(data_file)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    for base in (PLUGIN_ROOT, Path.cwd()):
        p = base / candidate
        if p.is_file():
            return p
    return None


def _iter_raw_entries(raw: Any):
    """Yield card entry dicts from the various accepted top-level shapes:
    a list of entries, a {name: entry} map, or {"cards": [...]} / {"data": [...]}.
    """
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                yield item
    elif isinstance(raw, dict):
        if isinstance(raw.get("cards"), list):
            yield from (i for i in raw["cards"] if isinstance(i, dict))
        elif isinstance(raw.get("data"), list):
            yield from (i for i in raw["data"] if isinstance(i, dict))
        else:
            # Treat as {name: entry}; inject the key as name if entry lacks one.
            for key, val in raw.items():
                if isinstance(val, dict):
                    val.setdefault("name", key)
                    yield val


def _load_index(settings: dict, logger: Optional[Any] = None) -> dict:
    """Load and index the data file as {normalized_name: [entries...]},
    re-reading on change."""
    path = _resolve_data_path(settings.get("data_file", "cards.json"))
    if path is None:
        _DATA_CACHE.update({"path": None, "mtime": None, "index": {}})
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _DATA_CACHE.get("index") or {}
    if _DATA_CACHE["index"] is not None and _DATA_CACHE["path"] == path \
            and _DATA_CACHE["mtime"] == mtime:
        return _DATA_CACHE["index"]

    # Three priority tiers: printed_name is matched before name, name before alias.
    index = {"printed": {}, "name": {}, "alias": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        count = 0
        for entry in _iter_raw_entries(raw):
            name = entry.get("name") or entry.get("card_name")
            if not name:
                continue
            printed = entry.get("printed_name", "")
            if printed:
                index["printed"].setdefault(_normalize(printed), []).append(entry)
            index["name"].setdefault(_normalize(name), []).append(entry)
            aliases = entry.get("aliases") or entry.get("alias") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            for a in aliases:
                if a:
                    index["alias"].setdefault(_normalize(a), []).append(entry)
            count += 1
        if settings.get("log", True) and logger is not None:
            logger.update(msg_success(f"[LocalJSON] Loaded {count} card(s) from {path.name}"))
    except Exception as e:
        if logger is not None:
            logger.update(msg_warn(f"[LocalJSON] Failed to read {path.name}: {e}"))
        index = {"printed": {}, "name": {}, "alias": {}}
    _DATA_CACHE.update({"path": path, "mtime": mtime, "index": index})
    return index


def _lookup(index: dict, card: dict, settings: dict) -> Optional[dict]:
    """Find the best matching entry for a card, trying printed_name, then name,
    then alias. Within a tier, [SET]/{num} tags disambiguate duplicates."""
    key = _normalize(card.get("name", ""))
    for tier in ("printed", "name", "alias"):
        entries = index.get(tier, {}).get(key)
        if entries:
            return _select_entry(entries, card, settings)
    return None


"""
* Entry -> Scryfall-shaped dict
"""


def _apply_aliases(entry: dict) -> dict:
    """Return a shallow copy with friendly aliases renamed to Scryfall keys.
    Canonical keys already present always win over their aliases."""
    out = dict(entry)
    for alias, canonical in _ALIASES.items():
        if alias in out and canonical not in out:
            out[canonical] = out.pop(alias)
        elif alias in out:
            out.pop(alias, None)
    return out


def _normalize_face(face: dict) -> dict:
    """Normalise a single card face, ensuring keys the layout code reads exist."""
    f = _apply_aliases(face)
    f.setdefault("name", "")
    f.setdefault("type_line", "")  # layout reads card['type_line'] directly
    f.setdefault("oracle_text", "")
    f.setdefault("mana_cost", "")
    for k in ("power", "toughness", "loyalty"):
        if k in f and f[k] is not None:
            f[k] = str(f[k])
    return f


def _expand_entry(entry: dict) -> dict:
    """Turn a stored entry into a Scryfall-shaped dict Proxyshop can consume.

    Raw Scryfall objects pass through essentially untouched (only missing
    defaults are filled). Friendly/custom entries are expanded via aliases and
    sensible defaults so any template can render them offline.
    """
    data = _apply_aliases(entry)
    # Drop our own bookkeeping flags if present.
    data.pop("_raw", None)

    # Multi-face cards (transform / mdfc / etc.)
    faces = data.get("card_faces")
    if isinstance(faces, list) and faces:
        data["card_faces"] = [_normalize_face(face) for face in faces]
        # Default the overall name to "Front // Back" if not supplied.
        if not data.get("name"):
            names = [f.get("name", "") for f in data["card_faces"]]
            data["name"] = " // ".join(n for n in names if n)
        # A DFC needs a layout; default to transform unless told otherwise.
        data.setdefault("layout", "transform")
    else:
        data.setdefault("layout", "normal")

    # Top-level defaults used across the layout/collector code.
    data.setdefault("name", entry.get("name", ""))
    data.setdefault("type_line", "")
    data.setdefault("oracle_text", "")
    data.setdefault("mana_cost", "")
    data.setdefault("rarity", "common")
    data.setdefault("set", "MTG")
    data.setdefault("lang", "en")
    data.setdefault("keywords", data.get("keywords", []))

    # Stringify numeric stats (Scryfall stores these as strings).
    for k in ("power", "toughness", "loyalty"):
        if k in data and data[k] is not None:
            data[k] = str(data[k])

    # Derive colour identity if missing (from cost / colours / faces).
    if "color_identity" not in data:
        face_costs = [f.get("mana_cost", "") for f in data.get("card_faces", [])]
        data["color_identity"] = _derive_color_identity(
            data.get("mana_cost", ""), data.get("colors"), *face_costs
        )

    return data


"""
* Overlay / merge support (for "merge" mode)
"""


def _is_empty(value: Any) -> bool:
    """True for None, blank/whitespace strings, and empty collections.
    Numbers (including 0) and booleans count as real, non-empty values."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _alias_face(face: dict) -> dict:
    """Apply friendly aliases to a face dict and stringify its stats,
    without injecting any defaults (used for overlay)."""
    f = _apply_aliases(face)
    for k in ("power", "toughness", "loyalty"):
        if k in f and f[k] is not None:
            f[k] = str(f[k])
    return f


def _normalize_overrides(entry: dict) -> dict:
    """Turn a stored entry into a set of override fields for merge mode:
    friendly aliases applied, stats stringified, no defaults injected, and
    matching-only/bookkeeping keys removed. `name` is intentionally dropped so
    it can't clobber Scryfall's canonical (and multi-face) name."""
    data = _apply_aliases(entry)
    for k in ("_raw", "_comment", "aliases", "alias", "card_name", "name"):
        data.pop(k, None)
    faces = data.get("card_faces")
    if isinstance(faces, list):
        data["card_faces"] = [_alias_face(f) for f in faces if isinstance(f, dict)]
    for k in ("power", "toughness", "loyalty"):
        if k in data and data[k] is not None:
            data[k] = str(data[k])
    return data


def _deep_merge(base: dict, overrides: dict) -> dict:
    """Overlay non-empty `overrides` onto `base`. Nested dicts merge
    recursively; `card_faces` lists merge per index (front over front, etc.)."""
    for key, value in overrides.items():
        if _is_empty(value):
            continue
        if key == "card_faces" and isinstance(value, list) and isinstance(base.get(key), list):
            faces = [dict(f) if isinstance(f, dict) else f for f in base[key]]
            for i, face_override in enumerate(value):
                if not isinstance(face_override, dict):
                    continue
                if i < len(faces) and isinstance(faces[i], dict):
                    faces[i] = _deep_merge(faces[i], face_override)
                else:
                    faces.append(face_override)
            base[key] = faces
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(dict(base[key]), value)
        else:
            base[key] = value
    return base


def _select_entry(entries: list, card: dict, settings: dict) -> Optional[dict]:
    """From entries sharing a name, pick the best match using [SET]/{num} tags."""
    if not entries:
        return None
    if len(entries) == 1:
        return entries[0]

    number = (card.get("number") or "").lstrip("0 ") or card.get("number")
    if settings.get("match_number", True) and number:
        for e in entries:
            cn = str(e.get("collector_number", e.get("number", ""))).lstrip("0 ")
            if cn and cn == number:
                return e

    code = (card.get("set") or "").lower()
    if settings.get("match_set", True) and code:
        for e in entries:
            if str(e.get("set", "")).lower() == code:
                return e

    return entries[0]


"""
* Replacement for src.cards.get_card_data
"""


def get_card_data_local(card: dict, cfg: Any, logger: Optional[Any] = None) -> Optional[dict]:
    """Drop-in replacement for Proxyshop's get_card_data.

    Tries the local JSON data file first (per settings); otherwise defers to the
    original Scryfall-backed implementation.
    """
    settings = _load_settings()

    # Master switch / disabled mode -> vanilla behaviour.
    if not settings.get("enabled", True) or settings.get("mode", "auto") == "off":
        return _ORIGINAL_GET_CARD_DATA(card, cfg, logger)

    index = _load_index(settings, logger)
    chosen = _lookup(index, card, settings)
    mode = settings.get("mode", "auto")
    log = settings.get("log", True) and logger is not None

    # Merge mode: always query Scryfall, then overlay non-empty JSON fields.
    if mode == "merge":
        base = _ORIGINAL_GET_CARD_DATA(card, cfg, logger)
        if chosen is None:
            return base
        if base is None:
            # Scryfall returned nothing; fall back to building from JSON alone.
            if log:
                logger.update(msg_warn(
                    f"[LocalJSON] Scryfall miss; building from local data: {card.get('name', '')}"))
            try:
                return _expand_entry(chosen)
            except Exception as e:
                if log:
                    logger.update(msg_warn(f"[LocalJSON] Bad entry for {card.get('name','')}: {e}"))
                return None
        try:
            merged = _deep_merge(base, _normalize_overrides(chosen))
            if log:
                logger.update(msg_success(f"[LocalJSON] Overrode Scryfall fields: {card.get('name', '')}"))
            return merged
        except Exception as e:
            if log:
                logger.update(msg_warn(f"[LocalJSON] Merge failed for {card.get('name','')}: {e}"))
            return base

    # auto / strict modes: serve the local entry directly when present.
    if chosen is not None:
        if log:
            logger.update(msg_success(f"[LocalJSON] Using local data: {card.get('name', '')}"))
        try:
            return _expand_entry(chosen)
        except Exception as e:
            if logger is not None:
                logger.update(msg_warn(f"[LocalJSON] Bad entry for {card.get('name','')}: {e}"))
            # Fall through to Scryfall in auto mode, fail in strict mode.

    # No local match.
    if mode == "strict":
        if log:
            logger.update(msg_warn(f"[LocalJSON] Not in local data (strict mode): {card.get('name', '')}"))
        return None

    # auto mode -> Scryfall fallback.
    return _ORIGINAL_GET_CARD_DATA(card, cfg, logger)


"""
* Patch installation
"""


def _install_patch() -> None:
    """Replace get_card_data everywhere it has been imported by name.

    layouts.py does `from src.cards import get_card_data`, binding the name in
    its own namespace, so patching src.cards alone is not enough. We rebind the
    name in src.cards and in every already-imported module that holds the
    original function object.
    """
    global _ORIGINAL_GET_CARD_DATA
    import src.cards as cards_mod

    if _ORIGINAL_GET_CARD_DATA is None:
        _ORIGINAL_GET_CARD_DATA = cards_mod.get_card_data

    # Patch the source of truth.
    cards_mod.get_card_data = get_card_data_local

    # Patch any module that imported the symbol by name (e.g. src.layouts).
    for module in list(sys.modules.values()):
        if module is None or module is cards_mod:
            continue
        try:
            if getattr(module, "get_card_data", None) is _ORIGINAL_GET_CARD_DATA:
                setattr(module, "get_card_data", get_card_data_local)
        except Exception:
            continue


# Install on import (this module is imported by Proxyshop at startup).
try:
    _install_patch()
    print("[LocalJSON] Local JSON card-data source installed.")
except Exception as _e:  # pragma: no cover
    print(f"[LocalJSON] Could not install patch: {_e}")
