#!/usr/bin/env python3
"""DRAFT: package/unpack a hash-anchored final export; never builds or publishes.

Install beside music_data.py only after review. Existing music_data.py remains the
authority for master/check/publish. No credentials are read by pack/unpack/verify.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

FORMAT = "ournotes.prebuilt-music-data/1"
SHA = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def publisher(path):
    spec = importlib.util.spec_from_file_location("music_data", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def safe_name(name):
    require(isinstance(name, str) and name and not any(c in name for c in ("\\", ":", "\x00")),
            "invalid archive path")
    path = PurePosixPath(name)
    require(not path.is_absolute() and all(p not in ("", ".", "..") for p in name.split("/")),
            "archive path escapes extraction root")
    require(name == "prebuilt.json" or name.startswith("data/"), "unexpected archive root")
    return path


def inventory_entry(path):
    return {"sha256": digest(path), "bytes": path.stat().st_size}


def validate_exporter_identity(root, proof, verify_raw_checkout=False):
    """Prove committed blobs and explicitly reconstruct producer byte identity in memory.

    Linux can use canonical bytes; a Windows checkout may declare observed
    LF-to-CRLF conversions. CI reconstructs only those conversions in memory.
    """
    identity = proof.get("exporterSourceIdentity", {})
    require(identity.get("format") == "ournotes.exporter-source-identity/1", "missing exporter source identity")
    paths = git(root, "ls-tree", "-r", "--name-only", "HEAD", "--", "src/nnnotes").splitlines()
    paths = sorted(p for p in paths if p.endswith(".py"))
    require(paths and len(paths) == identity.get("fileCount"), "exporter Python source count mismatch")
    blobs = {p: subprocess.check_output(["git", "-C", str(root), "show", "HEAD:" + p]) for p in paths}
    def tree_hash(items):
        values = {p: hashlib.sha256(raw).hexdigest() for p, raw in items.items()}
        return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    canonical = tree_hash(blobs)
    require(canonical == identity.get("canonicalGitTreeSha256"), "committed exporter source tree mismatch")
    normalized = {p: hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest() for p, raw in blobs.items()}
    normalized_tree = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    transforms = identity.get("checkoutTransforms")
    require(isinstance(transforms, list), "missing producer checkout transforms")
    seen = set()
    for transform in transforms:
        require(isinstance(transform, dict) and set(transform) == {"path", "operation"}
                and transform["operation"] == "lf-to-crlf" and transform["path"] in blobs
                and transform["path"] not in seen, "unexpected producer checkout transform")
        path = transform["path"]
        seen.add(path)
        source = blobs[path]
        require(b"\r" not in source, "canonical transformed source is not LF-only")
        blobs[path] = source.replace(b"\n", b"\r\n")
    raw_tree = tree_hash(blobs)
    require(raw_tree == identity.get("rawTreeSha256") == proof.get("exporterSourceTreeSha256"),
            "reconstructed producer byte tree mismatch")
    require(proof.get("exporterCommit") == git(root, "rev-parse", "HEAD"), "generation exporter commit mismatch")
    if verify_raw_checkout:
        actual = {p: (root / p).read_bytes() for p in paths}
        require(tree_hash(actual) == raw_tree, "actual pack producer byte tree differs from generation")
    return {"format": "ournotes.exporter-normalized-source/1", "scope": "committed Python files; CRLF-to-LF only",
            "fileCount": len(paths), "lfNormalizedTreeSha256": normalized_tree, "lfNormalizedFiles": normalized}


def validate_generation(proof, doc, main_sha):
    require(proof.get("format") == "ournotes.final-generation/1", "unknown generation proof")
    require(proof.get("sourceStableDuringRun") is True and proof.get("exporterStableDuringRun") is True,
            "producer source changed during generation")
    require(proof.get("unmetTargets") == 0, "generation contains unmet precision targets")
    require(proof.get("musicDataSha256") == main_sha, "generation/main SHA mismatch")
    require(proof.get("modelCommit") == doc["provenance"]["deck"]["commit"], "generation/model mismatch")
    require(proof.get("manifestSha256") == doc["replay"]["sha256"], "generation/runtime mismatch")
    require(proof.get("songs") == len(doc["songs"]), "generation/song count mismatch")
    require(proof.get("charts") == sum(len(s["charts"]) for s in doc["songs"]), "generation/chart count mismatch")
    require(proof.get("engine", {}).get("workingTreeDirty") is False, "generation engine is not explicitly clean")


def pack(args):
    require(REPOSITORY.fullmatch(args.producer_repository), "invalid producer repository")
    require(REPOSITORY.fullmatch(args.player_repository), "invalid player repository")
    require(COMMIT.fullmatch(args.player_commit), "player must be pinned to a full commit")
    root, data, jackets = args.producer.resolve(), args.data.resolve(), args.jackets.resolve()
    require(git(root, "rev-parse", "--is-shallow-repository") == "false", "producer needs full history")
    require(not git(root, "status", "--porcelain", "--untracked-files=all"), "producer checkout is dirty")
    producer_commit = git(root, "rev-parse", "HEAD")
    require(COMMIT.fullmatch(producer_commit), "invalid producer commit")
    module = publisher(args.publisher.resolve())
    require(module.nnnotes_commit(root) == producer_commit, "producer HEAD must be its latest source commit")
    doc = json.loads((data / "music-data.json").read_bytes())
    main_sha = digest(data / "music-data.json")
    require(module.deck_commit(root) == doc["provenance"]["deck"]["commit"], "producer pins another model")
    proof = json.loads(args.generation.read_bytes())
    validate_generation(proof, doc, main_sha)
    normalized_source = validate_exporter_identity(root, proof, verify_raw_checkout=True)
    resources = module.replay_resources(data, doc)
    require(resources, "runtime resources are mandatory")
    files = {"data/music-data.json": data / "music-data.json"}
    for path in resources:
        files["data/" + path.relative_to(data).as_posix()] = path
    for song in doc["songs"]:
        jacket = song["jacket"]
        require(isinstance(jacket, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", jacket), "invalid jacket name")
        path = jackets / f"{jacket}.webp"
        require(path.is_file() and not path.is_symlink() and path.stat().st_size > 0, f"missing jacket {jacket}")
        files[f"data/jackets/{jacket}.webp"] = path
    for name, path in files.items():
        safe_name(name)
        require(path.is_file() and not path.is_symlink(), f"payload is not a regular file: {name}")
    require("data/check.json" not in files and "data/build.json" not in files, "fresh CI check/marker required")
    metadata = {
        "format": FORMAT,
        "producer": {"repository": args.producer_repository, "commit": producer_commit,
                     "normalizedSourceIdentity": normalized_source},
        "player": {"repository": args.player_repository, "commit": args.player_commit},
        "deckCommit": module.deck_commit(root),
        "masterVersion": doc["provenance"]["master"]["version"],
        "musicDataSha256": main_sha,
        "replayManifestSha256": doc["replay"]["sha256"],
        "generation": proof,
        "files": {name: inventory_entry(path) for name, path in sorted(files.items())},
    }
    require(sum(item["bytes"] for item in metadata["files"].values()) < MAX_EXPANDED, "bundle too large")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    require(not args.output.exists(), "refusing to replace an existing bundle")
    with zipfile.ZipFile(args.output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("prebuilt.json", json.dumps(metadata, indent=2) + "\n")
        for name, path in sorted(files.items()):
            archive.write(path, name)
    require(args.output.stat().st_size <= MAX_ARCHIVE, "bundle archive too large")
    checksum = digest(args.output)
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(f"{checksum}  {args.output.name}\n", encoding="utf8")
    print(json.dumps({"bundle": args.output.name, "sha256": checksum, "bytes": args.output.stat().st_size,
                      "payloadFiles": len(files), "producerCommit": producer_commit, "playerCommit": args.player_commit}))


def unpack(args):
    require(SHA.fullmatch(args.sha256), "expected bundle SHA must be lowercase 64 hex")
    require(args.archive.stat().st_size <= MAX_ARCHIVE and digest(args.archive) == args.sha256,
            "bundle SHA/size mismatch")
    require(not args.out.exists(), "extraction destination must be new")
    with zipfile.ZipFile(args.archive) as archive:
        entries = archive.infolist()
        names = [i.filename for i in entries]
        require(len(names) == len(set(names)), "duplicate archive entry")
        require(len(entries) <= 2000 and sum(i.file_size for i in entries) <= MAX_EXPANDED, "archive exceeds bounds")
        for entry in entries:
            safe_name(entry.filename)
            require(not entry.is_dir() and not stat.S_ISLNK(entry.external_attr >> 16), "non-file archive entry")
        require("prebuilt.json" in names, "bundle has no identity metadata")
        require(archive.getinfo("prebuilt.json").file_size <= 1024 * 1024, "metadata too large")
        meta = json.loads(archive.read("prebuilt.json"))
        require(meta.get("format") == FORMAT, "unknown bundle format")
        require(meta["producer"]["repository"] == args.producer_repository, "unexpected producer repository")
        for key in ("producer", "player"):
            require(REPOSITORY.fullmatch(meta[key]["repository"]) and COMMIT.fullmatch(meta[key]["commit"]),
                    f"invalid {key} identity")
        expected = set(meta["files"]) | {"prebuilt.json"}
        require(set(names) == expected, "archive entries differ from payload inventory")
        require("data/check.json" not in names and "data/build.json" not in names,
                "bundle must not supply CI gate results or outer build marker")
        # All identities, names and sizes are validated before creating any files.
        for name, identity in meta["files"].items():
            safe_name(name)
            require(SHA.fullmatch(identity["sha256"]) and type(identity["bytes"]) is int
                    and identity["bytes"] == archive.getinfo(name).file_size, "invalid payload inventory")
        args.out.mkdir(parents=True)
        for entry in entries:
            dest = args.out / entry.filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read(entry))
            if entry.filename != "prebuilt.json":
                require(inventory_entry(dest) == meta["files"][entry.filename], "payload hash mismatch")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf8") as stream:
            for key in ("producer", "player"):
                stream.write(f"{key}_repository={meta[key]['repository']}\n{key}_commit={meta[key]['commit']}\n")
    print(json.dumps({"unpacked": True, "payloadFiles": len(meta["files"]), "sha256": args.sha256}))


def verify(args):
    meta = json.loads((args.bundle / "prebuilt.json").read_bytes())
    root = args.producer.resolve()
    require(meta["format"] == FORMAT, "unknown identity metadata")
    require(git(root, "rev-parse", "HEAD") == meta["producer"]["commit"], "producer checkout mismatch")
    require(not git(root, "status", "--porcelain", "--untracked-files=all"), "producer checkout is dirty")
    module = publisher(args.publisher.resolve())
    require(module.nnnotes_commit(root) == meta["producer"]["commit"], "latest NN source commit mismatch")
    require(module.deck_commit(root) == meta["deckCommit"], "Cargo.lock model mismatch")
    sys.path.insert(0, str(root / "src"))
    import nnnotes
    require(Path(nnnotes.__file__).resolve() == root / "src/nnnotes/__init__.py", "nnnotes import is not genuine producer source")
    doc = json.loads((args.bundle / "data/music-data.json").read_bytes())
    require(nnnotes.__version__ == doc["provenance"]["exporter"]["version"], "exporter version mismatch")
    for name, identity in meta["files"].items():
        require(inventory_entry(args.bundle / name) == identity, f"changed payload {name}")
    validate_generation(meta["generation"], doc, digest(args.bundle / "data/music-data.json"))
    normalized_source = validate_exporter_identity(root, meta["generation"])
    require(normalized_source == meta["producer"].get("normalizedSourceIdentity"), "per-file LF-normalized source differs")
    require(doc["provenance"]["master"]["version"] == meta["masterVersion"], "master identity mismatch")
    require(module.replay_resources(args.bundle / "data", doc), "missing runtime")
    print(json.dumps({"verified": True, "producerCommit": meta["producer"]["commit"], "deckCommit": meta["deckCommit"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack")
    for flag in ("producer", "data", "jackets", "generation", "publisher", "output"):
        p.add_argument("--" + flag, type=Path, required=True)
    p.add_argument("--producer-repository", default="MetaSekaiLab/nnnotes")
    p.add_argument("--player-repository", default="empty-sekai/ournotes-player")
    p.add_argument("--player-commit", required=True)
    p.set_defaults(func=pack)
    p = sub.add_parser("unpack")
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--producer-repository", default="MetaSekaiLab/nnnotes")
    p.set_defaults(func=unpack)
    p = sub.add_parser("verify")
    for flag in ("bundle", "producer", "publisher"):
        p.add_argument("--" + flag, type=Path, required=True)
    p.set_defaults(func=verify)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, KeyError, OSError, zipfile.BadZipFile) as error:
        parser.exit(1, f"prebuilt_publish: {error}\n")


if __name__ == "__main__":
    main()
