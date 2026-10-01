"""Compression and re-encoding invariants without Rust, credentials or game payloads."""
import gzip
import io
import json

import pytest

import http_compression as transport
import music_data as md
import music_data_reencode as reencode
import story_site
from test_music_data import FakeS3, FakeBucket, published_out, replay_out


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


def fixture(tmp_path, monkeypatch):
    s3 = FakeS3(); out, _ = published_out(tmp_path, monkeypatch, s3)
    doc, manifest = replay_out(out)
    labels = {"format": "nnnotes.replay-labels/1", "region": doc["provenance"]["region"],
        "masterVersion": doc["provenance"]["master"]["version"], "tables": {}}
    for name in md.REPLAY_LABEL_TABLES:
        labels["tables"][name] = {"sha256": "ab" * 32, "rows": []}
        doc["provenance"]["master"]["tables"][name] = {"sha256": "ab" * 32}
    label_raw = json.dumps(labels).encode()
    (out / "replay/snap-labels.json").write_bytes(label_raw)
    manifest["snapLabels"] = {"format": labels["format"], "url": "snap-labels.json", "sha256": md.sha256(label_raw), "bytes": len(label_raw)}
    manifest_raw = json.dumps(manifest).encode(); (out / "replay/manifest.json").write_bytes(manifest_raw)
    doc["replay"]["sha256"] = md.sha256(manifest_raw)
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
    assert {"replay/snap-labels.json", "replay/deck-data.json", "replay/manifest.json", "music-data.json", "build.json"} <= {item["key"] for item in report["objects"]}
    for full, raw in before.items():
        if full.endswith(".json"):
            assert s3.headers[full]["ContentEncoding"] == "gzip"
            assert gzip.decompress(s3.store[full]) == raw
        else:
            assert s3.store[full] == raw and "ContentEncoding" not in s3.headers[full]


@pytest.mark.parametrize("change", ["payload", "marker", "source", "labels"])
def test_reencoding_rejects_stale_source_changed_marker_or_broken_payload_before_writing(tmp_path, monkeypatch, change):
    s3, _ = fixture(tmp_path, monkeypatch)
    if change == "payload": s3.store["music-data/replay/deck-data.json"] = b"broken"
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
    if change == "labels": s3.store.pop("music-data/replay/snap-labels.json")
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
