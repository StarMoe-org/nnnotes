#!/usr/bin/env python3
"""Read the frozen published replay inputs for a UI-only smoke; never upload data."""
import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path, PurePosixPath

BASE = "https://storage.bdon.moe/moenotes/music-data/"
MAIN_SHA = "983896151d3ed69d9dfe26044225907e33be037e4b74589e110ded64780f8738"
MANIFEST_SHA = "8941cc7b2e146b3376dcbeefe9efab4a10b09845b9e0dad1ab76d1ee2b990dea"
MODEL = "dbd9cf01a4854808dceb6aedcd4041af372faeee"


def relative(value):
    if not isinstance(value, str) or not value or any(c in value for c in (":", "\\", "?", "#")):
        raise ValueError("resource URL must be a relative file path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in (".", "..") for p in path.parts):
        raise ValueError("resource URL escapes the replay directory")
    return path


def fetch(path, expected, destination, expected_bytes=None):
    request = urllib.request.Request(BASE + path.as_posix(), headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected:
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
    raw = fetch(PurePosixPath("music-data.json"), MAIN_SHA, out / "music-data.json")
    doc = json.loads(raw)
    pointer = doc["replay"]
    if pointer["sha256"] != MANIFEST_SHA:
        raise ValueError("published main names a different replay manifest")
    manifest_path = relative(pointer["manifestUrl"])
    manifest = json.loads(fetch(manifest_path, MANIFEST_SHA, out / str(manifest_path)))
    if (manifest["format"] != "nnnotes.replay-manifest/1" or
            manifest["engine"]["model"]["commit"] != MODEL or
            manifest["engine"]["requestFormat"] != "ournotes.replay/1"):
        raise ValueError("published engine identity/ABI differs from the frozen model")
    chart = [c for c in manifest["charts"] if c["scoreId"] == 10000200]
    if len(chart) != 1:
        raise ValueError("published manifest lacks the reviewed chart 10000200")
    resources = [manifest["deckData"], manifest["engine"]["js"], manifest["engine"]["wasm"],
                 manifest["engine"]["build"], chart[0]]
    for entry in resources:
        path = manifest_path.parent / relative(entry["url"])
        fetch(path, entry["sha256"], out / str(path), entry["bytes"])
    engine_build = json.loads((out / str(manifest_path.parent / relative(manifest["engine"]["build"]["url"]))).read_bytes())
    if (engine_build["commit"] != MODEL or engine_build.get("workingTreeDirty") is True or
            engine_build["jsSha256"] != manifest["engine"]["js"]["sha256"] or
            engine_build["wasmSha256"] != manifest["engine"]["wasm"]["sha256"]):
        raise ValueError("published engine build metadata is inconsistent")
    pin = args.out / "page-pin"
    pin.mkdir()
    (pin / "prebuilt.json").write_text(json.dumps({"player": {"commit": args.player_ref}}) + "\n")
    report = {"mainSha256": MAIN_SHA, "manifestSha256": MANIFEST_SHA, "modelCommit": MODEL,
              "playerCommit": args.player_ref, "downloadedResources": 7,
              "scope": "UI-only immutable consumer inputs; no data build or data upload"}
    (args.out / "data-check.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
