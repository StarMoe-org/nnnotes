"""Refresh and publication regressions without network, Rust or game resources."""
import copy
import json
from pathlib import Path

import pytest

import music_data
from test_music_data import FakeS3, context, gate, gates, published_out, sample


def test_same_version_table_change_rebuilds_but_check_timestamp_does_not(monkeypatch):
    monkeypatch.setenv("MASTERDATA_REGION", "jp")
    monkeypatch.setattr(music_data, "deck_commit", lambda root: "a" * 40)
    monkeypatch.setattr(music_data, "nnnotes_commit", lambda root: "b" * 40)
    entry = {"version": "same", "verified_at": "yesterday", "manifest_sha256": "c" * 64}
    files = {"MasterLiveScoreRank.json": "d" * 64, "MasterManifest.json": "c" * 64}
    before = music_data.inputs(entry, files=files)
    assert before == music_data.inputs(dict(entry, verified_at="today"), files=files)
    after = music_data.inputs(entry, files=dict(files, **{"MasterLiveScoreRank.json": "e" * 64}))
    assert before["masterVersion"] == after["masterVersion"]
    assert before["decodedFilesSha256"] != after["decodedFilesSha256"]
    assert music_data.inputs(dict(entry, manifest_sha256="f" * 64),
        files=dict(files, **{"MasterManifest.json": "f" * 64})) != before
    assert music_data.inputs(entry, files=dict(reversed(list(files.items())))) == before


def test_snapshot_identity_never_contains_private_service_configuration():
    entry = {"version": "v", "server": {"token": "private"}, "upstream": {"url": "private"},
             "assets": {"api_root": "private"}}
    identity = music_data.snapshot_identity("jp", entry, {"MasterManifest.json": "a" * 64})
    assert "private" not in json.dumps(identity)


@pytest.mark.parametrize("files,entry", [({}, {}), ({"table.json": "a" * 64}, {}),
    ({"MasterManifest.json": "a" * 64}, {"manifest_sha256": "b" * 64}),
    ({"MasterManifest.json": "invalid"}, {})])
def test_snapshot_identity_requires_a_consistent_manifest(files, entry):
    with pytest.raises(SystemExit, match="snapshot"):
        music_data.snapshot_identity("jp", entry, files)


def test_changed_consumer_pin_rebuilds(monkeypatch):
    monkeypatch.setenv("MASTERDATA_REGION", "jp")
    monkeypatch.setattr(music_data, "deck_commit", lambda root: "a" * 40)
    monkeypatch.setattr(music_data, "nnnotes_commit", lambda root: "b" * 40)
    monkeypatch.setenv("PLAYER_REPOSITORY", "example/player")
    monkeypatch.setenv("PLAYER_REF", "first")
    before = music_data.inputs({})
    monkeypatch.setenv("PLAYER_REF", "second")
    assert music_data.inputs({}) != before


@pytest.mark.parametrize("changed", ["tables", "region", "missing"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_stale_or_wrong_region_build_uploads_nothing(tmp_path, monkeypatch, changed, dry_run):
    s3 = FakeS3()
    out, _ = published_out(tmp_path, monkeypatch, s3)
    marker = json.loads((out / music_data.MARKER).read_bytes())
    if changed == "tables":
        marker["sourceSnapshot"]["decodedFilesSha256"] = "f" * 64
    elif changed == "region":
        marker["sourceSnapshot"]["masterRegion"] = "jp"
    else:
        marker.pop("sourceSnapshot")
    (out / music_data.MARKER).write_text(json.dumps(marker))
    with pytest.raises(SystemExit, match="source snapshot"):
        music_data.cmd_publish(str(out), dry_run=dry_run)
    assert not s3.store


def test_source_change_during_payload_upload_preserves_current_file(tmp_path, monkeypatch):
    s3 = FakeS3()
    out, _ = published_out(tmp_path, monkeypatch, s3)
    selected = copy.deepcopy(music_data.story_site.master_index()[1])
    calls = []
    def current():
        calls.append(True)
        if len(calls) == 2:
            selected["files"]["MasterLiveScoreRank.json"] = "f" * 64
        return "unused", selected
    monkeypatch.setattr(music_data.story_site, "master_index", current)
    s3.store["music-data/music-data.json"] = b"previous"
    s3.store["music-data/build.json"] = b"previous marker"
    with pytest.raises(SystemExit, match="source snapshot changed"):
        music_data.cmd_publish(str(out))
    assert len(calls) == 2
    assert s3.store["music-data/music-data.json"] == b"previous"
    assert s3.store["music-data/build.json"] == b"previous marker"
    assert any("archive/" in key for key in s3.store)


@pytest.mark.parametrize("field", ["requiredScore", "battleRequiredScore"])
def test_source_rank_gate_detects_stale_solo_and_room_fields(tmp_path, field):
    doc = sample()
    _, ctx = context(tmp_path, doc)
    doc["songs"][0]["scoreRanks"][1][field] += 1
    report = gates(json.dumps(doc).encode(), ctx, only=["sourceRanks"])
    assert not gate(report, "sourceRanks")["passed"]


def test_source_rank_gate_rejects_swapped_units(tmp_path):
    doc = sample()
    _, ctx = context(tmp_path, doc)
    for row in doc["songs"][0]["scoreRanks"]:
        row["requiredScore"], row["battleRequiredScore"] = row["battleRequiredScore"], row["requiredScore"]
    assert not gates(json.dumps(doc).encode(), ctx, only=["sourceRanks"])["passed"]


@pytest.mark.parametrize("event,payload,requested,expected", [
    ("schedule", None, "", ["hk-tw-mo", "jp"]),
    ("repository_dispatch", ["jp"], "", ["jp"]),
    ("repository_dispatch", None, "", ["hk-tw-mo", "jp"]),
    ("workflow_dispatch", None, "jp", ["jp"]),
])
def test_music_regions_select_both_by_default(tmp_path, monkeypatch, event, payload, requested, expected):
    event_file, output = tmp_path / "event.json", tmp_path / "output"
    event_file.write_text(json.dumps({"client_payload": {"regions": payload}}))
    monkeypatch.delenv("MUSIC_DATA_REGIONS", raising=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_file))
    monkeypatch.setenv("REQUESTED_REGION", requested)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    music_data.cmd_regions()
    assert output.read_text().splitlines() == [f"regions={json.dumps(expected)}", f"count={len(expected)}"]


def test_workflows_isolate_regions_and_share_publication_lock():
    root = Path(__file__).resolve().parents[1] / "workflows"
    wrapper = (root / "music-data.yml").read_text()
    normal = (root / "music-data-region.yml").read_text()
    prebuilt = (root / "music-data-prebuilt.yml").read_text()
    assert "fail-fast: false" in wrapper and "'hk-tw-mo jp'" in wrapper
    assert "group: music-data-${{ matrix.region }}" in wrapper
    assert "group: music-data-${{ inputs.region }}" in prebuilt
    for workflow in (normal, prebuilt):
        assert "MUSIC_DATA_JP_S3_PREFIX || 'jp/music-data'" in workflow
        assert "MUSIC_DATA_TW_S3_PREFIX || vars.MUSIC_DATA_S3_PREFIX || 'music-data'" in workflow
        assert "MASTERDATA_REGION: ${{ inputs.region }}" in workflow
        assert "MUSIC_DATA_MASTERDATA_REGION" not in workflow
        test_step = workflow[workflow.index("Gate self-test"):]
        assert "test_music_data_refresh.py" in test_step.split("- name:", 1)[0]
