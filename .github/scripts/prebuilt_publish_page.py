#!/usr/bin/env python3
"""DRAFT: upload only pinned examples/songs files to bucket root/songs/.

Run only after full music gates and the consumer smoke. Does not build player,
rewrite indexes, or touch any story/player asset outside songs/. HTML goes last.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--publisher", type=Path, required=True)
    p.add_argument("--page", type=Path, required=True)
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    meta = json.loads((a.bundle / "prebuilt.json").read_bytes())
    head = subprocess.check_output(["git", "-C", str(a.page), "rev-parse", "HEAD"], text=True).strip()
    if head != meta["player"]["commit"]:
        p.error("page HEAD differs from the final pinned player commit")
    root = Path(subprocess.check_output(["git", "-C", str(a.page), "rev-parse", "--show-toplevel"], text=True).strip())
    relative_page = a.page.resolve().relative_to(root.resolve()).as_posix()
    if subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all", "--", relative_page], text=True).strip():
        p.error("page files are dirty")
    files = sorted(a.page.iterdir())
    expected = {"index.html", "catalog.js", "ranking.js", "pareto.js", "songs.js", "text.js", "replay-panel.js", "replay-worker.js", "replay-preset.js"}
    if {v.name for v in files} != expected or any(not v.is_file() or v.is_symlink() for v in files):
        p.error("pinned songs module closure is not the reviewed nine files")
    spec = importlib.util.spec_from_file_location("music_data", a.publisher)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    files.sort(key=lambda path: (path.name == "index.html", path.name))
    dry = a.dry_run or not module.publishing()
    if dry:
        for file in files:
            print(f"would upload songs/{file.name} sha256={module.sha256(file.read_bytes())}")
        return
    bucket = module.story_site.Bucket()
    bucket.prefix = ""  # Parent requires bucket root/songs, independently of music-data's prefix.
    if not bucket.writable:
        p.error("page publishing requires the existing S3 write credentials")
    for file in files:
        key, checksum = "songs/" + file.name, module.sha256(file.read_bytes())
        module.upload(bucket, key, file, "no-cache")
        matched = False
        for attempt in range(4):
            try:
                if module.sha256(bucket.s3.get_object(Bucket=bucket.name, Key=key)["Body"].read()) == checksum:
                    matched = True
                    break
            except Exception as error:
                print(f"read back {key}: {type(error).__name__}")
            time.sleep(3 * (attempt + 1))
        if not matched:
            url = module.env("STORY_S3_ENDPOINT").rstrip("/") + "/" + module.env("STORY_S3_BUCKET") + "/" + key
            matched = module.sha256(module.get(url, timeout=300)) == checksum
        if not matched:
            p.error(f"page object read-back SHA mismatch: {key}")
    print(json.dumps({"published": True, "files": len(files), "prefix": "songs/", "playerCommit": head}))


if __name__ == "__main__":
    main()
