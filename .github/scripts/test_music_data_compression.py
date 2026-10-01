"""Compression and re-encoding invariants without Rust, credentials or game payloads."""
import gzip
import io
import json
from pathlib import Path
from pathlib import PurePosixPath
import sys

import pytest

import http_compression as transport
import music_data as md
import music_data_reencode as reencode
import story_site
import songs_page_data
from test_music_data import FakeS3, FakeBucket, published_out, replay_out, replay_path, replay_key, rewrite_replay_manifest


def test_json_transport_keeps_the_manifest_hash_and_has_deterministic_stored_identity(tmp_path):
    raw = b'{"repeated":"' + b"a" * 2000 + b'"}'
    path = tmp_path / "deck-data.json"; path.write_bytes(raw)
    s3 = FakeS3(); bucket = FakeBucket(s3)
    md.upload(bucket, "replay/deck-data.json", path, "no-cache")
    obj = s3.get_object(Bucket=bucket.name, Key=bucket.prefix + "replay/deck-data.json")
    facts = transport.verify_object(obj, md.sha256(raw), compressed_json=True)
    assert int(facts["encoded-bytes"]) < len(raw)
    assert s3.store[bucket.prefix + "replay/deck-data.json"] == transport.encode_json(raw)[0]
    assert gzip.decompress(s3.store[bucket.prefix + "replay/deck-data.json"]) == raw
    assert facts["decoded-sha256"] == md.sha256(raw)


def test_publisher_uses_the_real_bucket_contract_instead_of_a_fake_listing_api(tmp_path, monkeypatch):
    s3 = FakeS3(); out, _ = published_out(tmp_path, monkeypatch, s3)
    bucket = story_site.Bucket.__new__(story_site.Bucket)
    bucket.name, bucket.prefix, bucket.writable, bucket.s3 = "moenotes", "music-data/", True, s3
    assert not hasattr(bucket, "keys")
    monkeypatch.setattr(md, "bucket", lambda: bucket)
    md.cmd_publish(str(out))
    assert "music-data/music-data.json" in s3.store and "music-data/build.json" in s3.store
    assert bucket.object_size("music-data.json") == len(s3.store["music-data/music-data.json"])


def test_same_size_corrupt_archive_is_restored_and_verified_before_any_pointer(tmp_path, monkeypatch):
    s3 = FakeS3(); out, report = published_out(tmp_path, monkeypatch, s3)
    md.cmd_publish(str(out)); s3.log.clear()
    archive = f"music-data/archive/v-test/{report['sha256']}.json"
    previous = s3.store[archive]
    damaged = bytearray(previous); damaged[len(damaged) // 2] ^= 1
    s3.store[archive] = bytes(damaged)
    assert len(previous) == len(s3.store[archive])
    monkeypatch.setattr(transport, "get_object", lambda url, **kwargs: s3.get_object(Bucket="moenotes", Key="music-data/" + url.split("/music-data/", 1)[1]))
    md.cmd_publish(str(out))
    assert [key for key, _, _ in s3.log] == [archive, "music-data/music-data.json", "music-data/build.json"]
    assert s3.store[archive] == previous
    transport.verify_object(s3.get_object(Bucket="moenotes", Key=archive), report["sha256"], compressed_json=True)


@pytest.mark.parametrize("defect", ["missing-encoding", "wrong-type", "wrong-size", "wrong-stored-sha", "wrong-decoded-sha", "corrupt-body"])
def test_wrong_encoding_or_either_identity_is_rejected(defect):
    raw = b'{"test":true}'; body, extra = transport.encode_json(raw)
    obj = {"Body": io.BytesIO(body), "ContentType": "application/json", **extra}
    if defect == "missing-encoding": obj.pop("ContentEncoding")
    if defect == "wrong-type": obj["ContentType"] = "application/octet-stream"
    if defect == "wrong-size": obj["Metadata"]["encoded-bytes"] = "1"
    if defect == "wrong-stored-sha": obj["Metadata"]["encoded-sha256"] = "a" * 64
    if defect == "wrong-decoded-sha": obj["Metadata"]["decoded-sha256"] = "a" * 64
    if defect == "corrupt-body": obj["Body"] = io.BytesIO(b"broken")
    with pytest.raises((ValueError, OSError)):
        transport.verify_object(obj, md.sha256(raw), compressed_json=True)


def test_python_public_reads_decode_real_content_encoding(monkeypatch):
    raw = b'{"source":"unchanged"}'
    class Response:
        headers = {"Content-Encoding": "gzip"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return gzip.compress(raw)
    def urlopen(request, **kwargs):
        assert request.headers["Accept-encoding"] == "gzip"
        return Response()
    monkeypatch.setattr(story_site.urllib.request, "urlopen", urlopen)
    assert story_site.get("https://example.test/data.json") == raw


@pytest.mark.parametrize("corrupt", [False, True])
def test_ui_reader_checks_decoded_gzip_identity(monkeypatch, tmp_path, corrupt):
    raw = b'{"source":"current"}'
    class Response:
        headers = {"Content-Encoding": "gzip"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return gzip.compress(raw)
    def urlopen(request, **kwargs):
        assert request.headers["Accept-encoding"] == "gzip"
        return Response()
    monkeypatch.setattr(songs_page_data.urllib.request, "urlopen", urlopen)
    expected = "a" * 64 if corrupt else md.sha256(raw)
    if corrupt:
        with pytest.raises(ValueError, match="SHA/size mismatch"):
            songs_page_data.fetch(PurePosixPath("data.json"), expected, tmp_path / "data.json", len(raw))
    else:
        assert songs_page_data.fetch(PurePosixPath("data.json"), expected, tmp_path / "data.json", len(raw)) == raw


@pytest.mark.parametrize("defect", [None, "marker-race", "missing-gate", "labels"])
def test_ui_reader_freezes_new_snapshot_from_marker_archive_and_rejects_changed_identities(tmp_path, monkeypatch, defect):
    model = "a" * 40
    source = {"masterVersion": "new-86-song-master"}
    tables = {name: {"sha256": "b" * 64} for name in md.REPLAY_LABEL_TABLES}
    provenance = {"region": "tw", "master": {"version": source["masterVersion"], "tables": tables}, "deck": {"commit": model}}
    blobs = {}
    def resource(name, raw):
        blobs[name] = raw
        return {"url": name, "sha256": md.sha256(raw), "bytes": len(raw)}
    js = resource("engine/replay.js", b"export default ()=>{}"); wasm = resource("engine/replay.wasm", b"wasm")
    engine_build = resource("engine/build.json", json.dumps({"commit": model, "workingTreeDirty": False,
        "jsSha256": js["sha256"], "wasmSha256": wasm["sha256"]}).encode())
    deck = resource("deck-data.json", json.dumps({"provenance": provenance}).encode())
    labels = {"format": "nnnotes.replay-labels/1", "region": "tw", "masterVersion": source["masterVersion"],
        "tables": {name: {**table, "rows": []} for name, table in tables.items()}}
    if defect == "labels": labels["masterVersion"] = "old"
    label_entry = resource("snap-labels.json", json.dumps(labels).encode())
    chart = {"scoreId": 10000200, **resource("charts/10000200.json", b"{}")}
    manifest = {"format": "nnnotes.replay-manifest/1", "charts": [chart], "deckData": deck, "snapLabels": label_entry,
        "engine": {"model": {"commit": model}, "requestFormat": "ournotes.replay/1", "js": js, "wasm": wasm, "build": engine_build}}
    manifest_raw = json.dumps(manifest).encode(); manifest_sha = md.sha256(manifest_raw)
    runtime = f"replay/{manifest_sha}/"
    blobs = {runtime + key: value for key, value in blobs.items()}
    blobs[runtime + "manifest.json"] = manifest_raw
    doc = {"provenance": provenance, "songs": [{}] * 86,
        "replay": {"sha256": manifest_sha, "manifestUrl": runtime + "manifest.json"}}
    main_raw = json.dumps(doc).encode(); main_sha = md.sha256(main_raw)
    archive = f"archive/{source['masterVersion']}/{main_sha}.json"; blobs[archive] = main_raw
    marker = {"format": md.BUILD_FORMAT, "file": md.FILE, "archive": archive, "sha256": main_sha, "bytes": len(main_raw),
        "inputs": {"deckCommit": model}, "sourceSnapshot": source,
        "gates": {name: {"passed": True} for name in {name for name, _ in md.GATES} | {"replay"}}}
    if defect == "missing-gate": marker["gates"].pop("replay")
    blobs[md.MARKER] = json.dumps(marker).encode(); requested = []
    class Response:
        headers = {"Content-Encoding": "gzip"}
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return gzip.compress(self.body)
    def urlopen(request, **kwargs):
        key = request.full_url.removeprefix(songs_page_data.BASE); requested.append(key)
        if defect == "marker-race" and key == md.MARKER and requested.count(key) > 1: return Response(b'{}')
        return Response(blobs[key])
    monkeypatch.setattr(songs_page_data.urllib.request, "urlopen", urlopen)
    out = tmp_path / "ui"
    monkeypatch.setattr(sys, "argv", ["songs_page_data", "--player-ref", "c" * 40, "--out", str(out)])
    if defect:
        with pytest.raises(ValueError): songs_page_data.main()
        assert not (out / "page-pin/prebuilt.json").exists()
    else:
        songs_page_data.main()
        report = json.loads((out / "data-check.json").read_bytes())
        assert report["mainSha256"] == main_sha and report["manifestSha256"] == manifest_sha
        assert (out / "data/music-data.json").read_bytes() == main_raw
        assert md.FILE not in requested and archive in requested and requested.count(md.MARKER) == 2
        assert report["sourceSnapshot"] == source


def fixture(tmp_path, monkeypatch):
    s3 = FakeS3(); out, _ = published_out(tmp_path, monkeypatch, s3)
    doc, manifest = replay_out(out)
    labels = {"format": "nnnotes.replay-labels/1", "region": doc["provenance"]["region"],
        "masterVersion": doc["provenance"]["master"]["version"], "tables": {}}
    for name in md.REPLAY_LABEL_TABLES:
        labels["tables"][name] = {"sha256": "ab" * 32, "rows": []}
        doc["provenance"]["master"]["tables"][name] = {"sha256": "ab" * 32}
    label_raw = json.dumps(labels).encode()
    replay_path(out, doc, "snap-labels.json").write_bytes(label_raw)
    manifest["snapLabels"] = {"format": labels["format"], "url": "snap-labels.json", "sha256": md.sha256(label_raw), "bytes": len(label_raw)}
    rewrite_replay_manifest(out, doc, manifest)
    raw = json.dumps(doc).encode(); (out / md.FILE).write_bytes(raw)
    marker = json.loads((out / md.MARKER).read_bytes())
    marker.update(format=md.BUILD_FORMAT, file=md.FILE, sha256=md.sha256(raw), bytes=len(raw),
        inputs={"deckCommit": doc["provenance"]["deck"]["commit"]},
        gates={name: {"passed": True} for name in [*[name for name, _ in md.GATES], "replay"]})
    (out / md.MARKER).write_text(json.dumps(marker))
    for path in [out / md.FILE, out / md.MARKER, *md.replay_resources(out, doc)]:
        key = "music-data/" + path.relative_to(out).as_posix()
        s3.store[key] = path.read_bytes(); s3.headers[key] = {"ContentType": "application/json" if path.suffix == ".json" else "application/octet-stream"}
    s3.store["music-data/" + marker["archive"]] = raw
    s3.headers["music-data/" + marker["archive"]] = {"ContentType": "application/json"}
    def published(key):
        full = "music-data/" + key
        return transport.decode_content(s3.store[full], s3.headers[full].get("ContentEncoding")) if full in s3.store else None
    monkeypatch.setattr(md, "published", published)
    monkeypatch.setattr(transport, "get_object", lambda url, **kwargs: s3.get_object(Bucket="moenotes", Key="music-data/" + url.split("/music-data/", 1)[1]))
    return s3, marker


def test_reencoding_changes_all_json_transport_and_preserves_wasm_js_and_every_decoded_hash(tmp_path, monkeypatch):
    s3, _ = fixture(tmp_path, monkeypatch); before = dict(s3.store)
    report = reencode.reencode(tmp_path / "encoding")
    assert report["encodedBytes"] < report["decodedBytes"]
    assert {"snap-labels.json", "deck-data.json", "manifest.json", "music-data.json", "build.json"} <= {Path(item["key"]).name for item in report["objects"]}
    for full, raw in before.items():
        if full.endswith(".json"):
            assert s3.headers[full]["ContentEncoding"] == "gzip"
            assert gzip.decompress(s3.store[full]) == raw
        else:
            assert s3.store[full] == raw and "ContentEncoding" not in s3.headers[full]


@pytest.mark.parametrize("change", ["payload", "marker", "source", "labels"])
def test_reencoding_rejects_stale_source_changed_marker_or_broken_payload_before_writing(tmp_path, monkeypatch, change):
    s3, _ = fixture(tmp_path, monkeypatch)
    doc = json.loads(md.published(md.FILE))
    if change == "payload": s3.store[replay_key(doc, "deck-data.json")] = b"broken"
    if change == "marker":
        original = md.published; count = 0
        def published(key):
            nonlocal count
            if key == md.MARKER:
                count += 1
                if count > 1: return b'{}'
            return original(key)
        monkeypatch.setattr(md, "published", published)
    if change == "source": monkeypatch.setattr(md, "require_current_source", lambda marker: md.fail("source snapshot changed"))
    if change == "labels": s3.store.pop(replay_key(doc, "snap-labels.json"))
    with pytest.raises((ValueError, SystemExit)):
        reencode.reencode(tmp_path / "encoding")
    assert s3.log == []


def test_reencoding_aborts_when_pointer_changes_after_an_upload(tmp_path, monkeypatch):
    s3, _ = fixture(tmp_path, monkeypatch); original = md.upload
    def upload(*args):
        original(*args)
        s3.store["music-data/build.json"] = b'{}'
    monkeypatch.setattr(md, "upload", upload)
    with pytest.raises(ValueError, match="published build changed"):
        reencode.reencode(tmp_path / "encoding")
    assert len(s3.log) == 1


def test_reencoding_dry_run_performs_validation_but_no_writes(tmp_path, monkeypatch):
    s3, _ = fixture(tmp_path, monkeypatch)
    assert reencode.reencode(tmp_path / "encoding", dry_run=True)["dryRun"] is True
    assert s3.log == []
