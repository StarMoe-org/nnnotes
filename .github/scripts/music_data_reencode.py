#!/usr/bin/env python3
"""Re-encode a verified published snapshot without generating or changing its decoded payloads."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import http_compression
import music_data as md


def require(condition, message):
    if not condition:
        raise ValueError(message)


def target(root: Path, base: Path, name: str) -> Path:
    require(isinstance(name, str) and name and not any(c in name for c in ":\\?#") and not Path(name).is_absolute(),
            "object URL must be a relative path")
    path = (base / name).resolve()
    require(path.is_relative_to(root), "object URL leaves the selected prefix")
    return path


def reencode(out: Path, dry_run=False) -> dict:
    md.require_distinct_region_prefixes()
    require(not out.exists(), "output directory must be new")
    marker_raw = md.published(md.MARKER)
    require(marker_raw is not None, "published build marker is missing")
    marker = json.loads(marker_raw)
    require(marker.get("format") == md.BUILD_FORMAT and marker.get("file") == md.FILE
            and md.SHA256.fullmatch(str(marker.get("sha256"))), "published build identity is invalid")
    required_gates = {name for name, _ in md.GATES} | {"replay"}
    require(all((marker.get("gates", {}).get(name) or {}).get("passed") is True for name in required_gates),
            "published marker does not attest all current gates")
    marker_sha = md.sha256(marker_raw)

    def current():
        md.require_current_source(marker)
        latest = md.published(md.MARKER)
        require(latest is not None and md.sha256(latest) == marker_sha, "published build changed during re-encoding")

    current()
    root = out.resolve()
    root.mkdir(parents=True)
    (root / md.MARKER).write_bytes(marker_raw)

    def fetch(name, expected, size=None):
        current()  # Source and published marker are checked before every payload read.
        raw = md.published(name)
        require(raw is not None and md.sha256(raw) == expected and (size is None or len(raw) == size),
                f"published decoded payload differs: {name}")
        path = target(root, root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    file = fetch(md.FILE, marker["sha256"], marker["bytes"])
    doc = json.loads(file.read_bytes())
    require(doc.get("format") == md.FORMAT and doc["provenance"]["master"]["version"] == marker["sourceSnapshot"]["masterVersion"]
            and doc["provenance"]["deck"]["commit"] == marker["inputs"]["deckCommit"], "document differs from build identity")
    pointer = doc.get("replay") or {}
    manifest_path = fetch(pointer["manifestUrl"], pointer["sha256"])
    manifest = json.loads(manifest_path.read_bytes())
    require("snapLabels" in manifest, "all 13 same-source Snap label tables are required")
    engine = manifest["engine"]
    for entry in [manifest["deckData"], manifest["snapLabels"], *manifest["charts"], engine["js"], engine["wasm"], engine["build"]]:
        path = target(root, manifest_path.parent, entry["url"])
        fetch(path.relative_to(root).as_posix(), entry["sha256"], entry["bytes"])
    runtime = md.replay_resources(root, doc)  # Includes labels/source hashes, chart IDs, model and JS/WASM build identity.
    archive = fetch(marker["archive"], marker["sha256"], marker["bytes"])
    paths = [archive, *[path for path in runtime if path.suffix.lower() == ".json"], file, root / md.MARKER]
    unique = list(dict.fromkeys(paths))
    report = {"format": "moenotes.music-data-encoding/1", "dryRun": bool(dry_run or not md.publishing()),
        "musicDataSha256": marker["sha256"], "buildMarkerSha256": marker_sha, "sourceSnapshot": marker["sourceSnapshot"],
        "contentEncoding": "gzip", "objects": [], "unchangedArtifacts": [path.relative_to(root).as_posix() for path in runtime if path.suffix.lower() != ".json"]}
    bucket = md.bucket() if not report["dryRun"] else None
    if bucket is not None:
        require(bucket.writable, "publication credentials are unavailable")
        # Prove the store/CDN serves gzip correctly at a new public probe before replacing any existing data object.
        probe_raw = json.dumps({"format": "moenotes.gzip-probe/1", "musicDataSha256": marker["sha256"]}, sort_keys=True).encode()
        probe_key = f"encoding-probe/{md.sha256(probe_raw)}.json"
        probe_path = root / "transport-probe.json"; probe_path.write_bytes(probe_raw)
        current(); md.upload(bucket, probe_key, probe_path, md.ARCHIVE_CACHE)
        current(); md.read_back(bucket, probe_key, md.sha256(probe_raw))
        current()
        report["probe"] = {"key": probe_key, **http_compression.verify_object(
            http_compression.get_object(md.public_url(probe_key)), md.sha256(probe_raw), compressed_json=True)}
    for path in unique:
        key = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        _, transport = http_compression.encode_json(raw)
        facts = {"key": key, **transport["Metadata"]}
        if bucket is not None:
            current()  # Re-encoding never overlaps another source/pointer revision.
            md.upload(bucket, key, path, md.ARCHIVE_CACHE if key == marker["archive"] else md.FILE_CACHE)
            current()
            md.read_back(bucket, key, md.sha256(raw))
            current()
            # Independently verify the externally served Content-Encoding, Content-Type and encoded/decoded identities.
            verified = http_compression.verify_object(http_compression.get_object(md.public_url(key)), md.sha256(raw), compressed_json=True)
            require(verified == transport["Metadata"], f"public transport differs from deterministic upload: {key}")
        report["objects"].append(facts)
    current()
    report["decodedBytes"] = sum(int(item["decoded-bytes"]) for item in report["objects"])
    report["encodedBytes"] = sum(int(item["encoded-bytes"]) for item in report["objects"])
    (root / "encoding-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md.summary(f"- {'would gzip' if report['dryRun'] else 'published gzip'} {len(unique)} JSON objects: "
               f"{report['decodedBytes']} decoded bytes → {report['encodedBytes']} encoded bytes; decoded SHA identities unchanged")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        reencode(args.out, args.dry_run)
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f"music_data_reencode: {error}\n")


if __name__ == "__main__":
    main()
