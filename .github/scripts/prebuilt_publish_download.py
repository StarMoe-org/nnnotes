#!/usr/bin/env python3
"""DRAFT: read a same-repository draft Release asset with GH_TOKEN via gh.

The expected SHA is a dispatch input approved independently of release metadata.
Tokens and temporary redirect URLs are never printed or written by this script.
"""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


def api(endpoint, output=None):
    command = ["gh", "api", endpoint]
    if output is None:
        return json.loads(subprocess.check_output(command))
    with output.open("xb") as stream:
        subprocess.run(command + ["-H", "Accept: application/octet-stream"], stdout=stream, check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repository", required=True)
    p.add_argument("--release-id", required=True)
    p.add_argument("--asset-name", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    if not (re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", a.repository)
            and re.fullmatch(r"[0-9]+", a.release_id)
            and re.fullmatch(r"[A-Za-z0-9_.-]+\.zip", a.asset_name)
            and re.fullmatch(r"[0-9a-f]{64}", a.sha256)):
        p.error("invalid repository, release ID, asset name or SHA")
    release = api(f"repos/{a.repository}/releases/{a.release_id}")
    if release.get("draft") is not True:
        p.error("asset must belong to a draft release")
    assets = [v for v in release["assets"] if v["name"] == a.asset_name]
    checksums = [v for v in release["assets"] if v["name"] == a.asset_name + ".sha256"]
    if len(assets) != 1 or len(checksums) != 1 or assets[0].get("state") != "uploaded":
        p.error("exactly one completed ZIP and checksum asset are required")
    if not (0 < assets[0]["size"] <= 512 * 1024 * 1024 and checksums[0]["size"] < 1024):
        p.error("unexpected release asset size")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    api(f"repos/{a.repository}/releases/assets/{assets[0]['id']}", a.out)
    checksum_path = a.out.with_suffix(a.out.suffix + ".sha256")
    api(f"repos/{a.repository}/releases/assets/{checksums[0]['id']}", checksum_path)
    actual = hashlib.sha256(a.out.read_bytes()).hexdigest()
    checksum = checksum_path.read_text(encoding="utf8").strip()
    if actual != a.sha256 or checksum != f"{a.sha256}  {a.asset_name}" or a.out.stat().st_size != assets[0]["size"]:
        p.error("release asset/checksum differ from the independently pinned bundle SHA")
    print(json.dumps({"draftRelease": True, "asset": a.asset_name, "sha256": actual, "bytes": assets[0]["size"]}))


if __name__ == "__main__":
    main()
