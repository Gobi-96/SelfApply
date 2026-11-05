"""Local multi-profile registry and isolated data directories.

The registry is intentionally small and contains display metadata only. Every
piece of private job-search state lives below the selected profile directory.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_PROFILES = 5
PROFILE_COLORS = ("#3975eb", "#0f9f76", "#8b5cf6", "#e87924", "#db4261")
SHARED_NAMES = {"backups", "control-token", "profiles", "profiles.json"}


def root_dir() -> Path:
    return Path(os.environ.get("SELFAPPLY_HOME", Path.home() / ".selfapply"))


def registry_path(root: Path | None = None) -> Path:
    return (root or root_dir()) / "profiles.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_registry(value: dict, root: Path | None = None) -> None:
    path = registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)
    if os.name != "nt":
        path.chmod(0o600)


def _display_name_from_legacy(root: Path) -> str:
    try:
        profile = json.loads((root / "profile.json").read_text(encoding="utf-8"))
        full_name = str(profile.get("personal", {}).get("full_name", "")).strip()
        if full_name:
            return full_name
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return "My profile"


def _new_entry(name: str, *, color_index: int, profile_id: str | None = None) -> dict:
    return {
        "id": profile_id or uuid.uuid4().hex[:12],
        "name": name.strip() or "New profile",
        "color": PROFILE_COLORS[color_index % len(PROFILE_COLORS)],
        "created_at": _now(),
        "updated_at": _now(),
    }


def ensure_registry(root: Path | None = None) -> dict:
    """Create the registry and migrate legacy flat data without deleting it."""
    root = root or root_dir()
    path = registry_path(root)
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("profiles") and value.get("active_profile_id"):
                return value
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

    entry = _new_entry(_display_name_from_legacy(root), color_index=0, profile_id="primary")
    destination = root / "profiles" / entry["id"]
    destination.mkdir(parents=True, exist_ok=True)
    # Migration is a same-volume rename where possible. Shared root metadata
    # and prior backups remain outside profiles.
    for child in list(root.iterdir()):
        if child.name in SHARED_NAMES:
            continue
        target = destination / child.name
        if target.exists():
            continue
        shutil.move(str(child), str(target))
    value = {"version": 1, "active_profile_id": entry["id"], "profiles": [entry]}
    _write_registry(value, root)
    return value


def active_profile(root: Path | None = None) -> dict:
    registry = ensure_registry(root)
    active_id = registry["active_profile_id"]
    return next(item for item in registry["profiles"] if item["id"] == active_id)


def active_data_dir(root: Path | None = None) -> Path:
    root = root or root_dir()
    return root / "profiles" / active_profile(root)["id"]


def initials(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    if not words:
        return "?"
    return (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()


def application_count(data_dir: Path) -> int:
    path = data_dir / "selfapply.db"
    if not path.exists():
        return 0
    try:
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE apply_status = 'applied'"
            ).fetchone()
            return int(row[0] if row else 0)
    except sqlite3.Error:
        return 0


def profile_full_name(data_dir: Path) -> str:
    try:
        value = json.loads((data_dir / "profile.json").read_text(encoding="utf-8"))
        return str(value.get("personal", {}).get("full_name", "")).strip()
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def list_profiles(root: Path | None = None) -> dict:
    root = root or root_dir()
    registry = ensure_registry(root)
    rows = []
    for item in registry["profiles"]:
        row = dict(item)
        data_dir = root / "profiles" / row["id"]
        row["initials"] = initials(profile_full_name(data_dir) or row["name"])
        row["active"] = row["id"] == registry["active_profile_id"]
        row["application_count"] = application_count(data_dir)
        rows.append(row)
    return {"profiles": rows, "active_profile_id": registry["active_profile_id"], "limit": MAX_PROFILES}


def create_profile(name: str, root: Path | None = None) -> dict:
    root = root or root_dir()
    registry = ensure_registry(root)
    if len(registry["profiles"]) >= MAX_PROFILES:
        raise ValueError(f"You can create up to {MAX_PROFILES} profiles.")
    entry = _new_entry(name, color_index=len(registry["profiles"]))
    (root / "profiles" / entry["id"]).mkdir(parents=True, exist_ok=False)
    registry["profiles"].append(entry)
    registry["active_profile_id"] = entry["id"]
    _write_registry(registry, root)
    return entry


def switch_profile(profile_id: str, root: Path | None = None) -> dict:
    root = root or root_dir()
    registry = ensure_registry(root)
    entry = next((item for item in registry["profiles"] if item["id"] == profile_id), None)
    if not entry:
        raise ValueError("That profile no longer exists.")
    registry["active_profile_id"] = profile_id
    entry["updated_at"] = _now()
    _write_registry(registry, root)
    return entry


def update_profile(profile_id: str, *, name: str, color: str | None = None,
                   root: Path | None = None) -> dict:
    root = root or root_dir()
    registry = ensure_registry(root)
    entry = next((item for item in registry["profiles"] if item["id"] == profile_id), None)
    if not entry:
        raise ValueError("That profile no longer exists.")
    entry["name"] = name.strip() or entry["name"]
    if color in PROFILE_COLORS:
        entry["color"] = color
    entry["updated_at"] = _now()
    _write_registry(registry, root)
    return entry


def delete_profile(profile_id: str, root: Path | None = None) -> dict:
    root = root or root_dir()
    registry = ensure_registry(root)
    entry = next((item for item in registry["profiles"] if item["id"] == profile_id), None)
    if not entry:
        raise ValueError("That profile no longer exists.")
    shutil.rmtree(root / "profiles" / profile_id, ignore_errors=True)
    registry["profiles"] = [item for item in registry["profiles"] if item["id"] != profile_id]
    if not registry["profiles"]:
        replacement = _new_entry("New profile", color_index=0)
        (root / "profiles" / replacement["id"]).mkdir(parents=True, exist_ok=True)
        registry["profiles"] = [replacement]
    if registry["active_profile_id"] == profile_id:
        registry["active_profile_id"] = registry["profiles"][0]["id"]
    _write_registry(registry, root)
    return entry
