#!/usr/bin/env python3
"""Read the frozen published replay inputs for a UI-only smoke; never upload data."""
import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path, PurePosixPath

from http_compression import decode_content
from music_data import GATES, REPLAY_LABEL_TABLES, check_engine_build

BASE = "https://storage.bdon.moe/moenotes/music-data/"


def relative(value):
    if not isinstance(value, str) or not value or any(c in value for c in (":", "\\", "?", "#")):
        raise ValueError("resource URL must be a relative file path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in (".", "..") for p in path.parts):
        raise ValueError("resource URL escapes the replay directory")
    return path


def fetch(path, expected, destination, expected_bytes=None):
    request = urllib.request.Request(BASE + path.as_posix(), headers={"Cache-Control": "no-cache", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=120) as response:
        encoded = response.read(64 * 1024 * 1024 + 1)
        if len(encoded) > 64 * 1024 * 1024:
            raise ValueError(f"published encoded resource exceeds size limit: {path}")
        raw = decode_content(encoded, response.headers.get("Content-Encoding"))
    if len(raw) > 64 * 1024 * 1024 or (expected is not None and hashlib.sha256(raw).hexdigest() != expected):
        raise ValueError(f"published resource SHA/size mismatch: {path}")
    if expected_bytes is not None and len(raw) != expected_bytes:
        raise ValueError(f"published resource byte count mismatch: {path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--player-ref", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.player_ref):
        parser.error("player_ref must be a full lowercase 40-hex commit")
    if args.out is None:
        print("player_ref is a full immutable commit")
        return
    if args.out.exists():
        parser.error("output must be a fresh directory")
    out = args.out / "data"
    marker_raw = fetch(PurePosixPath("build.json"), None, out / "build.json")
    marker = json.loads(marker_raw)
    if (marker.get("format") != "moenotes.music-data-build/1" or marker.get("file") != "music-data.json"
            or not re.fullmatch(r"[0-9a-f]{64}", str(marker.get("sha256")))
            or type(marker.get("bytes")) is not int or marker["bytes"] <= 0
            or any((marker.get("gates", {}).get(name) or {}).get("passed") is not True
                   for name in {name for name, _ in GATES} | {"replay"})):
        raise ValueError("published build marker lacks verified source identity and gates")
    main_sha = marker["sha256"]
    archive_path = relative(marker["archive"])
    if archive_path.parts[0] != "archive" or archive_path.name != main_sha + ".json":
        raise ValueError("published build archive is not content-addressed")
    raw = fetch(archive_path, main_sha, out / "music-data.json", marker["bytes"])
    doc = json.loads(raw)
    pointer = doc["replay"]
    model = marker["inputs"]["deckCommit"]
    if (not re.fullmatch(r"[0-9a-f]{40}", str(model)) or doc["provenance"]["deck"]["commit"] != model
            or doc["provenance"]["master"]["version"] != marker["sourceSnapshot"]["masterVersion"]):
        raise ValueError("published main and build source/model identities differ")
    manifest_sha = pointer["sha256"]
    manifest_path = relative(pointer["manifestUrl"])
    if str(manifest_path) != f"replay/{manifest_sha}/manifest.json":
        raise ValueError("published replay manifest is not content-addressed")
    manifest = json.loads(fetch(manifest_path, manifest_sha, out / str(manifest_path)))
    if (manifest["format"] != "nnnotes.replay-manifest/1" or
            manifest["engine"]["model"]["commit"] != model or
            manifest["engine"]["requestFormat"] != "ournotes.replay/1"):
        raise ValueError("published engine identity/ABI differs from the frozen model")
    chart = [c for c in manifest["charts"] if c["scoreId"] == 10000200]
    if len(chart) != 1:
        raise ValueError("published manifest lacks the reviewed chart 10000200")
    resources = [manifest["deckData"], manifest["engine"]["js"], manifest["engine"]["wasm"],
                 manifest["engine"]["build"], chart[0]]
    if "snapLabels" not in manifest:
        raise ValueError("published snapshot lacks the same-source Snap labels")
    resources.append(manifest["snapLabels"])
    for entry in resources:
        path = manifest_path.parent / relative(entry["url"])
        fetch(path, entry["sha256"], out / str(path), entry["bytes"])
    deck = json.loads((out / str(manifest_path.parent / relative(manifest["deckData"]["url"]))).read_bytes())
    labels = json.loads((out / str(manifest_path.parent / relative(manifest["snapLabels"]["url"]))).read_bytes())
    p = doc["provenance"]
    if (deck["provenance"]["region"] != p["region"] or deck["provenance"]["master"]["version"] != p["master"]["version"]
            or deck["provenance"]["deck"]["commit"] != model or labels.get("format") != "nnnotes.replay-labels/1"
            or labels.get("region") != p["region"] or labels.get("masterVersion") != p["master"]["version"]
            or any((labels.get("tables", {}).get(name) or {}).get("sha256") != p["master"]["tables"][name]["sha256"]
                   or not isinstance((labels.get("tables", {}).get(name) or {}).get("rows"), list)
                   for name in REPLAY_LABEL_TABLES)):
        raise ValueError("published deck/labels differ from the frozen source")
    check_engine_build("replay", manifest["engine"], json.loads(
        (out / str(manifest_path.parent / relative(manifest["engine"]["build"]["url"]))).read_bytes()))
    marker_sha = hashlib.sha256(marker_raw).hexdigest()
    fetch(PurePosixPath("build.json"), marker_sha, args.out / "marker-after.json", len(marker_raw))
    pin = args.out / "page-pin"
    pin.mkdir()
    (pin / "prebuilt.json").write_text(json.dumps({"player": {"commit": args.player_ref}}) + "\n")
    report = {"mainSha256": main_sha, "manifestSha256": manifest_sha, "modelCommit": model,
              "buildMarkerSha256": marker_sha, "sourceSnapshot": marker["sourceSnapshot"],
              "playerCommit": args.player_ref, "downloadedResources": 3 + len(resources),
              "scope": "UI-only immutable consumer inputs; no data build or data upload"}
    (args.out / "data-check.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
