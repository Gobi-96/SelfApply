import json
from pathlib import Path

import pytest

from selfapply import profiles


def test_legacy_data_migrates_into_primary_profile(tmp_path):
    (tmp_path / "profile.json").write_text(
        json.dumps({"personal": {"full_name": "Sample Person"}}), encoding="utf-8"
    )
    (tmp_path / "selfapply.db").write_bytes(b"legacy")
    (tmp_path / "control-token").write_text("shared", encoding="utf-8")

    registry = profiles.ensure_registry(tmp_path)

    assert registry["active_profile_id"] == "primary"
    assert registry["profiles"][0]["name"] == "Sample Person"
    assert (tmp_path / "profiles/primary/profile.json").exists()
    assert (tmp_path / "profiles/primary/selfapply.db").exists()
    assert (tmp_path / "control-token").read_text(encoding="utf-8") == "shared"


def test_profiles_are_isolated_and_limited_to_five(tmp_path):
    profiles.ensure_registry(tmp_path)
    for number in range(2, 6):
        profiles.create_profile(f"Person {number}", tmp_path)
    with pytest.raises(ValueError, match="up to 5"):
        profiles.create_profile("Too many", tmp_path)
    listing = profiles.list_profiles(tmp_path)
    assert len(listing["profiles"]) == 5
    assert all(item["application_count"] == 0 for item in listing["profiles"])
    assert len({item["color"] for item in listing["profiles"]}) == 5
    assert all((tmp_path / "profiles" / item["id"]).is_dir() for item in listing["profiles"])


def test_deleting_last_profile_creates_blank_replacement(tmp_path):
    registry = profiles.ensure_registry(tmp_path)
    profiles.delete_profile(registry["active_profile_id"], tmp_path)
    listing = profiles.list_profiles(tmp_path)
    assert len(listing["profiles"]) == 1
    assert listing["profiles"][0]["name"] == "New profile"
    assert listing["profiles"][0]["active"] is True
