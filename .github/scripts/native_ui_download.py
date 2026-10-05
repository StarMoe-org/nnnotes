"""Fetch the fixed same-repository draft release without logging credentials.

The package SHA is fixed independently in the isolated publication workflow.
Archive members are checked before writing a fresh output directory.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

REPOSITORY = "StarMoe-org/nnnotes"
ASSET_NAME = "native-ui-jp-07ae20b7.zip"
BUNDLE_SHA = "e73cd1b0d4e8c729b0cdc308a58fe286e383ed88d920472e02116f5250a436ef"
BUNDLE_BYTES = 969863


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def gh_api(endpoint, out=None):
    command = ["gh", "api", endpoint]
    if out is None:
        return json.loads(subprocess.check_output(command, stderr=subprocess.PIPE))
    with out.open("xb") as stream:
        subprocess.run(command + ["-H", "Accept: application/octet-stream"],
                       stdout=stream, stderr=subprocess.PIPE, check=True)


def extract(archive_path, out):
    require(not out.exists(), "output directory already exists")
    raw = archive_path.read_bytes()
    require(len(raw) == BUNDLE_BYTES and hashlib.sha256(raw).hexdigest() == BUNDLE_SHA,
            "bundle differs from independently pinned identity")
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        require(len(infos) == 18 and len({i.filename for i in infos}) == 18, "archive member count differs")
        require(sum(i.file_size for i in infos) < 1000000, "archive expanded size differs")
        for info in infos:
            name = info.filename
            require(not info.is_dir() and not name.startswith("/") and ":" not in name
                    and "\\" not in name and all(p not in ("", ".", "..") for p in name.split("/")),
                    "unsafe archive member")
            require(name == "publication-inventory.json" or name.startswith("encoded-payload/"),
                    "unexpected archive member")
            require((info.external_attr >> 16) & 0o170000 != 0o120000, "archive symlink forbidden")
        out.mkdir(parents=True)
        for info in infos:
            path = out.joinpath(*PurePosixPath(info.filename).parts)
            require(path.resolve().is_relative_to(out.resolve()), "archive member escaped output")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(archive.read(info))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.release_id:
            require(bool(re.fullmatch(r"[0-9]+", args.release_id)), "invalid release ID")
            release = gh_api(f"repos/{REPOSITORY}/releases/{args.release_id}")
            require(release.get("draft") is True, "release must remain a draft")
            assets = {v["name"]: v for v in release["assets"]}
            require(ASSET_NAME in assets and ASSET_NAME + ".sha256" in assets, "release assets missing")
            asset, checksum = assets[ASSET_NAME], assets[ASSET_NAME + ".sha256"]
            require(asset.get("state") == "uploaded" and asset["size"] == BUNDLE_BYTES
                    and 0 < checksum["size"] < 1024, "release asset metadata differs")
            args.archive.parent.mkdir(parents=True, exist_ok=True)
            gh_api(f"repos/{REPOSITORY}/releases/assets/{asset['id']}", args.archive)
            checksum_path = args.archive.with_name(args.archive.name + ".sha256")
            gh_api(f"repos/{REPOSITORY}/releases/assets/{checksum['id']}", checksum_path)
            require(checksum_path.read_text(encoding="utf8").strip() == f"{BUNDLE_SHA}  {ASSET_NAME}",
                    "release checksum differs")
        extract(args.archive, args.out)
        print(json.dumps({"packageSha256": BUNDLE_SHA, "bytes": BUNDLE_BYTES, "archiveMembers": 18}))
    except Exception as error:
        report = {"complete": False, "errorType": type(error).__name__}
        if isinstance(error, ValueError):
            report["reason"] = str(error)
        print(json.dumps(report))
        sys.exit(1)


if __name__ == "__main__":
    main()
