"""Observe real committed/checkout bytes; no Windows-only identity is assumed."""
import hashlib
import json
import subprocess

import pytest

import prebuilt_publish as prebuilt


def tree_hash(files):
    entries = {path: hashlib.sha256(raw).hexdigest() for path, raw in files.items()}
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def producer(tmp_path):
    files = {"src/nnnotes/a.py": b"VALUE = 1\n", "src/nnnotes/b.py": b"VALUE = 2\n"}
    for path, raw in files.items():
        file = tmp_path / path; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(raw)
    for args in [("init", "-q"), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.test"),
                 ("config", "core.autocrlf", "false"), ("add", "src"), ("commit", "-qm", "fixture")]:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True)
    return tmp_path, files


def proof(root, files, transforms):
    raw_files = dict(files)
    for transform in transforms:
        if transform.get("path") in raw_files:
            raw_files[transform["path"]] = raw_files[transform["path"]].replace(b"\n", b"\r\n")
    raw_sha = tree_hash(raw_files)
    return {"exporterCommit": prebuilt.git(root, "rev-parse", "HEAD"), "exporterSourceTreeSha256": raw_sha,
        "exporterSourceIdentity": {"format": "ournotes.exporter-source-identity/1", "fileCount": len(files),
            "canonicalGitTreeSha256": tree_hash(files), "rawTreeSha256": raw_sha, "checkoutTransforms": transforms}}


def test_canonical_linux_checkout_has_no_transforms_and_actual_file_count(producer):
    root, files = producer
    normalized = prebuilt.validate_exporter_identity(root, proof(root, files, []), verify_raw_checkout=True)
    assert normalized["fileCount"] == 2
    assert normalized["lfNormalizedTreeSha256"] == tree_hash(files)


def test_explicit_windows_line_endings_are_reconstructed_and_actual_bytes_verified(producer):
    root, files = producer
    changes = [{"path": "src/nnnotes/b.py", "operation": "lf-to-crlf"}]
    evidence = proof(root, files, changes)
    (root / "src/nnnotes/b.py").write_bytes(files["src/nnnotes/b.py"].replace(b"\n", b"\r\n"))
    assert prebuilt.validate_exporter_identity(root, evidence, verify_raw_checkout=True)["lfNormalizedTreeSha256"] == tree_hash(files)
    (root / "src/nnnotes/b.py").write_bytes(b"MODIFIED = 3\r\n")
    with pytest.raises(ValueError, match="actual pack producer byte tree differs"):
        prebuilt.validate_exporter_identity(root, evidence, verify_raw_checkout=True)


@pytest.mark.parametrize("changes", [[{"path": "src/nnnotes/missing.py", "operation": "lf-to-crlf"}],
    [{"path": "src/nnnotes/a.py", "operation": "edit"}], [{"path": "src/nnnotes/a.py", "operation": "lf-to-crlf"}] * 2])
def test_unknown_or_duplicate_transform_is_rejected(producer, changes):
    root, files = producer
    with pytest.raises(ValueError, match="unexpected producer checkout transform"):
        prebuilt.validate_exporter_identity(root, proof(root, files, changes))


def test_unobserved_transform_or_changed_committed_count_cannot_fake_identity(producer):
    root, files = producer
    evidence = proof(root, files, [])
    evidence["exporterSourceIdentity"]["fileCount"] += 1
    with pytest.raises(ValueError, match="source count mismatch"):
        prebuilt.validate_exporter_identity(root, evidence)
    evidence = proof(root, files, [])
    evidence["exporterSourceIdentity"]["rawTreeSha256"] = "a" * 64
    with pytest.raises(ValueError, match="producer byte tree mismatch"):
        prebuilt.validate_exporter_identity(root, evidence)
