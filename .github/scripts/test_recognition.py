"""Recognition gallery workflow: catalog, build, ordered create-only publication and the pointer, on synthetic data."""
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).parent))
import recognition as r

MASTER = "https://master.example"
ASSETS = "https://assets.example"
STORE = "https://store.example/bucket/"


def webp(width, height, seed):
    rng = np.random.default_rng(seed)
    pixels = rng.integers(0, 256, (height // 8, width // 8, 3), dtype=np.uint8).repeat(8, 0).repeat(8, 1)
    out = io.BytesIO()
    Image.fromarray(pixels).save(out, "WEBP", lossless=True)
    return out.getvalue()


def member(card_id, rarity=2):
    return {"_id": card_id, "_assetID": card_id, "_characterID": card_id, "_rarity": rarity, "_cardType": 1}


def snap(card_id):
    return {"_id": card_id, "_assetID": card_id, "_characterIDs": [1, card_id], "_rarity": 10, "_cardType": 2}


class World:
    """Master data service, asset service, public bucket and an S3 writer, all in memory."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.masters = {"hk-tw-mo": {"member": [member(1), member(2)], "snap": [snap(1)]},
                        "jp": {"member": [member(1), member(2)], "snap": [snap(1), snap(2)]}}
        self.art = {}
        self.store = {}
        self.puts = []
        self.corrupt_downloads = set()
        self.runtime = {}
        files = {}
        for index, (name, ext) in enumerate((("opencv/opencv.js", "js"), ("opencv/opencv_js.wasm", "wasm"),
                                             ("fields/ort.wasm.min.js", "js"), ("fields/ort-wasm-simd-threaded.mjs", "mjs"),
                                             ("fields/ort-wasm-simd-threaded.wasm", "wasm"), ("fields/fields.onnx", "onnx"))):
            raw = f"runtime {name} {index}".encode()
            self.runtime[name] = raw
            files[name] = {"sha256": r.digest(raw), "bytes": len(raw), "ext": ext}
        self.spec_path = tmp_path / "recognition-runtime.json"
        self.spec_path.write_text(json.dumps({"format": "moenotes.recognition-runtime/1", "release": "recognition-runtime-1",
                                              "source": "https://example.org/source", "files": files,
                                              "galleries": {"gallery": {"builder": "sift-rootsift/1", "requirements": []}}}))

    def put_runtime(self, names=None):
        for name, raw in self.runtime.items():
            if names is None or name in names:
                record = r.runtime_records(r.runtime_spec(self.spec_path))[name]
                self.store[record["path"]] = (raw, {"content-type": record["contentType"]})

    def index(self):
        regions = {}
        for region, tables in self.masters.items():
            files = {f"{r.KINDS[kind]['table']}.json": r.digest(self.table(region, kind)) for kind in tables}
            regions[region] = {"path": f"/{region.split('-')[0]}/master/", "entry": {"version": f"v-{region}"}, "files": files}
        return json.dumps({"schema_version": 1, "regions": regions}).encode()

    def table(self, region, kind):
        return json.dumps({"_allData": self.masters[region][kind]}).encode()

    def artwork(self, kind, asset):
        key = (kind, asset)
        if key not in self.art:
            self.art[key] = webp(384, 384, asset) if kind == "member" else webp(512, 288, 100 + asset)
        return self.art[key]

    def fetch(self, url, *, origin=False, missing_ok=False, decode=True, timeout=120):
        def respond(body, headers=None):
            return r.Response(200, headers or {}, body)
        if url == f"{MASTER}/index.json":
            return respond(self.index())
        if url.startswith(MASTER):
            for region in self.masters:
                for kind in self.masters[region]:
                    if url == f"{MASTER}/{region.split('-')[0]}/master/{r.KINDS[kind]['table']}.json":
                        return respond(self.table(region, kind))
        if url.startswith(ASSETS + "/files/"):
            name = url.removeprefix(ASSETS + "/files/")
            kind, asset = ("member" if name[0] == "m" else "snap"), int(name[1:])
            raw = self.artwork(kind, asset)
            return respond(raw + b"x" if (kind, asset) in self.corrupt_downloads else raw)
        if url.startswith(ASSETS + "/"):
            directory = url.removeprefix(ASSETS + "/")
            parts = directory.strip("/").split("/")
            kind = "member" if parts[2] == "MemberCard" else "snap"
            raw = self.artwork(kind, int(parts[3]))
            name = r.KINDS[kind]["file"]
            entry = {"path": f"/{directory}{name}", "file": f"/files/{kind[0]}{parts[3]}", "sha256": r.digest(raw),
                     "bytes": len(raw), "metadata": {"width": 384 if kind == "member" else 512, "height": 384 if kind == "member" else 288}}
            return respond(json.dumps({"files": [entry, dict(entry)]}).encode())
        if url.startswith(STORE):
            path = url.removeprefix(STORE)
            if path not in self.store:
                if missing_ok:
                    return None
                raise r.Failure(f"HTTP 404: {url}")
            body, headers = self.store[path]
            return respond(body, {"access-control-allow-origin": "*", "cache-control": "max-age=0", **headers})
        raise AssertionError(url)

    def put_object(self, **options):
        path = options["Key"]
        self.puts.append(dict(options, Body=None))
        if options.get("IfNoneMatch") == "*" and path in self.store:
            error = Exception("exists")
            error.response = {"Error": {"Code": "PreconditionFailed"}}
            raise error
        self.store[path] = (options["Body"], {"content-type": options["ContentType"], "cache-control": options["CacheControl"]})


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    for name, value in {"STORY_S3_ENDPOINT": "https://store.example", "STORY_S3_BUCKET": "bucket", "RECOGNITION_S3_PREFIX": "",
                        "RECOGNITION_REGIONS": "hk-tw-mo jp", "RECOGNITION_ASSET_API": ASSETS,
                        "MASTERDATA_BASE_URL": MASTER, "GITHUB_REPOSITORY": "example/nnnotes"}.items():
        monkeypatch.setenv(name, value)
    for name in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "FORCE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(r, "RUNTIME_SPEC", w.spec_path)
    monkeypatch.setattr(r, "fetch", w.fetch)
    monkeypatch.setattr(r, "object_present", lambda record: record["path"] in w.store)
    monkeypatch.setattr(r, "check_requirements", lambda spec: None)
    monkeypatch.setattr(r, "s3_client", lambda: w)
    monkeypatch.setattr(r.time, "sleep", lambda seconds: None)

    def features(images):
        rows, points, owners = [], [], []
        for index, image in enumerate(images):
            value = float(image.mean()) / 255
            rows.append(np.full((2, 128), value, np.float32))
            points.extend([(1.5, 2.0), (image.shape[1] - 1.0, image.shape[0] - 1.0)])
            owners.extend([index, index])
        return np.vstack(rows), np.array(points, np.float32), np.array(owners, np.int32), "test"

    monkeypatch.setattr(r, "sift_features", features)
    w.put_runtime()
    return w


def build(world, name="out", runtime_dir=None):
    out = world.tmp / name
    r.cmd_build(out, runtime_dir)
    return out


def bundle_of(out):
    pointer = json.loads((out / "site" / r.POINTER).read_text())
    return pointer, json.loads((out / "site" / f"assets/{pointer['bundle']['sha256']}.json").read_text())


def test_canonical_json_writes_integral_numbers_as_javascript_does():
    assert r.canonical({"b": 1, "a": [0.0, 384.0, 47.5, "x"]}) == b'{"a":[0,384,47.5,"x"],"b":1}'
    assert r.document({"box": [0.0, 1.25]}) == b'{\n  "box": [\n    0,\n    1.25\n  ]\n}\n'


def test_catalog_is_the_union_with_the_first_regions_identity(world):
    world.masters["jp"]["member"][1] = member(2, rarity=3)
    cards, warnings = r.catalog(r.master_sources(r.regions()))
    assert [(c["kind"], c["id"], c["source"]) for c in cards] == [
        ("member", "1", "hk-tw-mo"), ("member", "2", "hk-tw-mo"), ("snap", "1", "hk-tw-mo"), ("snap", "2", "jp")]
    assert cards[0]["regions"] == ["hk-tw-mo", "jp"] and cards[1]["regions"] == ["hk-tw-mo"]
    assert cards[3]["identity"] == {"assetId": "2", "characterIds": ["1", "2"], "rarity": 10, "cardType": 2}
    assert warnings == ["member:2: identity in jp differs from hk-tw-mo"]


def test_unknown_region_and_master_hash_changes_stop(world, monkeypatch):
    monkeypatch.setenv("RECOGNITION_REGIONS", "hk-tw-mo xx")
    with pytest.raises(r.Failure, match="unknown region"):
        r.regions()
    monkeypatch.setenv("RECOGNITION_REGIONS", "jp")
    index = world.index()
    monkeypatch.setattr(world, "index", lambda: index)
    world.masters["jp"]["snap"].append(snap(3))
    with pytest.raises(r.Failure, match="SHA-256 differs from index.json"):
        r.master_sources(r.regions())


def test_art_listing_requires_one_identity_per_path(world, monkeypatch):
    card = {"kind": "snap", "id": "1", "identity": {"assetId": "1"}, "source": "jp"}
    entry = r.art_entry(ASSETS, card)
    assert entry["assetPath"] == "jp/ja/SupportCard/1/snap_thumbnail/snap_thumbnail.webp"
    listing = {"files": [{"path": "/jp/ja/SupportCard/1/snap_thumbnail/snap_thumbnail.webp", "file": "/files/a", "sha256": "a" * 64, "bytes": 1},
                         {"path": "/jp/ja/SupportCard/1/snap_thumbnail/snap_thumbnail.webp", "file": "/files/b", "sha256": "b" * 64, "bytes": 1}]}
    monkeypatch.setattr(r, "fetch", lambda url, **kw: r.Response(200, {}, json.dumps(listing).encode()))
    with pytest.raises(r.Failure, match="differing listings"):
        r.art_entry(ASSETS, card)
    monkeypatch.setattr(r, "fetch", lambda url, **kw: r.Response(200, {}, b'{"files": []}'))
    with pytest.raises(r.Failure, match="lists no"):
        r.art_entry(ASSETS, card)


def test_member_crop_is_imageops_fit():
    image = Image.fromarray(np.random.default_rng(1).integers(0, 256, (500, 300, 3), dtype=np.uint8))
    for source in (image, image.transpose(Image.Transpose.TRANSPOSE), image.resize((384, 384))):
        box = r.fit_box(*source.size)
        explicit = source.resize(r.MEMBER_SIZE, Image.Resampling.LANCZOS, box=tuple(box))
        assert np.array_equal(np.asarray(explicit), np.asarray(ImageOps.fit(source, r.MEMBER_SIZE, Image.Resampling.LANCZOS)))


def test_build_writes_a_closed_content_addressed_bundle(world):
    out = build(world)
    pointer, bundle = bundle_of(out)
    site = out / "site"
    assert pointer["format"] == "moenotes.recognition-pointer/1" and bundle["previous"] is None
    assert bundle["counts"] == {"member": 2, "snap": 2}
    gallery = json.loads((site / bundle["files"]["gallery/manifest.json"]["path"]).read_text())
    claimed = gallery.pop("galleryId")
    assert r.digest(r.canonical(gallery)) == claimed
    assert [f"{c['kind']}:{c['id']}" for c in gallery["cards"]] == ["member:1", "member:2", "snap:1", "snap:2"]
    member_card = gallery["cards"][0]
    assert (member_card["width"], member_card["height"]) == r.MEMBER_SIZE
    assert member_card["art"]["derive"] == {"method": "fit", "box": [47.65957446808511, 0, 336.3404255319149, 384], "filter": "lanczos3"}
    assert member_card["art"]["file"] == member_card["art"]["sha256"] + ".webp"
    assert gallery["cards"][3]["art"]["assetPath"] == "jp/ja/SupportCard/2/snap_thumbnail/snap_thumbnail.webp"
    owners = np.frombuffer((site / "assets" / gallery["buffers"]["owners"]["file"]).read_bytes(), "<i4")
    assert owners.tolist() == [0, 0, 1, 1, 2, 2, 3, 3] and gallery["buffers"]["descriptors"]["shape"] == [8, 128]
    fields = json.loads((site / bundle["files"]["fields/manifest.json"]["path"]).read_text())
    assert fields["format"] == "ournotes.browser-cultivation-assets/2" and set(fields["runtime"]) == {"glue", "module", "wasm"}
    assert fields["model"]["size"] == len(world.runtime["fields/fields.onnx"])
    for name, record in bundle["files"].items():
        assert record["path"] == f"assets/{record['sha256']}.{record['path'].rsplit('.', 1)[1]}"
        assert record["contentType"] == r.TYPES[record["path"].rsplit(".", 1)[1]]
        assert (site / record["path"]).is_file() != (name in r.runtime_records(r.runtime_spec()))
    report = json.loads((out / "report.json").read_text())
    assert report["bundle"] == pointer["bundle"] and report["counts"] == {"member": 2, "snap": 2}


def test_runtime_dir_adds_the_pinned_files_for_a_local_preview(world):
    runtime = world.tmp / "runtime"
    for name, raw in world.runtime.items():
        (runtime / name).parent.mkdir(parents=True, exist_ok=True)
        (runtime / name).write_bytes(raw)
    out = build(world, runtime_dir=runtime)
    _, bundle = bundle_of(out)
    assert all((out / "site" / record["path"]).is_file() for record in bundle["files"].values())


def test_a_failed_artwork_check_builds_nothing(world):
    world.corrupt_downloads.add(("snap", 2))
    with pytest.raises(r.Failure, match="downloaded bytes differ"):
        build(world)
    assert not (world.tmp / "out" / "site" / r.POINTER).exists()


def test_publication_uploads_dependencies_then_manifests_then_bundle_then_pointer(world):
    out = build(world)
    pointer, bundle = bundle_of(out)
    r.cmd_publish(out, dry_run=False)
    keys = [put["Key"] for put in world.puts]
    assert keys[-1] == r.POINTER and world.puts[-1]["CacheControl"] == "no-cache" and "IfNoneMatch" not in world.puts[-1]
    manifests = {bundle["files"][name]["path"] for name in bundle["entries"].values()}
    bundle_path = f"assets/{pointer['bundle']['sha256']}.json"
    assert keys[-2] == bundle_path
    assert set(keys[-4:-2]) == manifests
    assert all(put["IfNoneMatch"] == "*" and put["CacheControl"] == r.ASSET_CACHE for put in world.puts[:-1])
    runtime_paths = {record["path"] for record in r.runtime_records(r.runtime_spec()).values()}
    assert not runtime_paths & set(keys)
    assert world.store[r.POINTER][0] == (out / "site" / r.POINTER).read_bytes()
    report = json.loads((out / "publication.json").read_text())
    assert report["complete"] and report["pointerWritten"]
    assert {row["state"] for row in report["objects"]} == {"uploaded", "existing"}


def test_partial_publication_resumes_without_rewrites(world):
    out = build(world)
    _, bundle = bundle_of(out)
    arts = [record for name, record in bundle["files"].items() if name.startswith("art/")]
    for record in arts[:2]:
        world.store[record["path"]] = ((out / "site" / record["path"]).read_bytes(), {"content-type": record["contentType"]})
    r.cmd_publish(out, dry_run=False)
    assert not {record["path"] for record in arts[:2]} & {put["Key"] for put in world.puts}
    world.puts.clear()
    r.cmd_publish(out, dry_run=False)          # again: everything is there and the pointer already names it
    assert world.puts == []


def test_an_existing_object_with_other_bytes_or_type_stops_before_any_write(world, monkeypatch):
    out = build(world)
    _, bundle = bundle_of(out)
    record = bundle["files"]["art/snap/1.webp"]
    world.store[record["path"]] = (b"other", {"content-type": "image/webp"})
    monkeypatch.setattr(r, "s3_client", lambda: pytest.fail("no write identity before the checks pass"))
    with pytest.raises(r.Failure, match="bytes differ"):
        r.cmd_publish(out, dry_run=False)
    world.store[record["path"]] = ((out / "site" / record["path"]).read_bytes(), {"content-type": "application/octet-stream"})
    with pytest.raises(r.Failure, match="Content-Type"):
        r.cmd_publish(out, dry_run=False)
    assert world.puts == []


def test_missing_runtime_files_stop_publication_but_not_a_dry_run(world, monkeypatch):
    out = build(world)
    world.store.clear()
    monkeypatch.setattr(r, "s3_client", lambda: pytest.fail("a dry run gets no write identity"))
    r.cmd_publish(out, dry_run=True)
    report = json.loads((out / "publication.json").read_text())
    assert len(report["unavailableRuntime"]) == 6 and report["counts"]["existing"] == 0
    with pytest.raises(r.Failure, match="runtime file"):
        r.cmd_publish(out, dry_run=False)
    assert world.puts == []


def test_a_pointer_moved_since_the_build_is_not_overwritten(world):
    stale = build(world, "stale")
    world.art[("snap", 1)] = webp(512, 288, 999)
    r.cmd_publish(build(world, "newer"), dry_run=False)
    world.puts.clear()
    with pytest.raises(r.Failure, match="pointer changed since this bundle was built"):
        r.cmd_publish(stale, dry_run=False)
    assert world.puts == []


def test_next_bundle_links_the_previous_and_keeps_every_object(world):
    first = build(world, "first")
    r.cmd_publish(first, dry_run=False)
    before = dict(world.store)
    world.masters["hk-tw-mo"]["member"].append(member(3))
    second = build(world, "second")
    pointer, bundle = bundle_of(second)
    assert bundle["previous"] == bundle_of(first)[0]["bundle"]
    r.cmd_publish(second, dry_run=False)
    assert all(world.store[path] == value for path, value in before.items() if path != r.POINTER)
    assert json.loads(world.store[r.POINTER][0])["bundle"] == pointer["bundle"]
    report = json.loads((second / "report.json").read_text())
    assert report["newCards"] == ["member:3"]


def test_a_catalog_that_loses_a_published_card_is_not_built_or_published(world):
    first = build(world, "first")
    r.cmd_publish(first, dry_run=False)
    world.masters["jp"]["snap"].pop()
    with pytest.raises(r.Failure, match="lacks published card"):
        build(world, "second")
    with pytest.raises(r.Failure, match="lacks published card"):
        r.cmd_plan()


def test_plan_builds_only_when_inputs_change(world, monkeypatch):
    outputs = {}
    monkeypatch.setattr(r, "output", outputs.__setitem__)
    r.cmd_plan()
    assert outputs == {"build": "true", "runtime": "false"}
    r.cmd_publish(build(world), dry_run=False)
    r.cmd_plan()
    assert outputs["build"] == "false"
    monkeypatch.setenv("FORCE", "true")
    r.cmd_plan()
    assert outputs["build"] == "true"
    monkeypatch.delenv("FORCE")
    world.art[("snap", 1)] = webp(512, 288, 999)
    r.cmd_plan()
    assert outputs["build"] == "true"


def test_runtime_job_uploads_only_missing_pins_from_the_release(world, monkeypatch):
    spec = r.runtime_spec()
    missing = ["fields/fields.onnx", "opencv/opencv_js.wasm"]
    for name in missing:
        del world.store[r.runtime_records(spec)[name]["path"]]
    assets = [{"id": index, "name": f"{record['sha256']}.{record['ext']}", "size": record["bytes"], "state": "uploaded"}
              for index, record in enumerate(spec["files"].values())]
    monkeypatch.setattr(r, "gh_json_lines", lambda args: [{"id": 1, "draft": True, "assets": assets}])
    by_id = {index: world.runtime[name] for index, name in enumerate(spec["files"])}
    monkeypatch.setattr(r, "gh_download", lambda repository, asset_id, target: target.write_bytes(by_id[asset_id]))
    r.cmd_runtime(world.tmp / "work-dry", world.tmp / "dry.json", dry_run=True)
    assert world.puts == []
    r.cmd_runtime(world.tmp / "work", world.tmp / "runtime.json", dry_run=False)
    assert sorted(put["Key"] for put in world.puts) == sorted(r.runtime_records(spec)[name]["path"] for name in missing)
    report = json.loads((world.tmp / "runtime.json").read_text())
    assert report["complete"] and sum(row["state"] == "uploaded" for row in report["objects"]) == 2


def test_unknown_gallery_builders_are_rejected(world):
    spec = json.loads(world.spec_path.read_text())
    spec["galleries"]["gallery"]["builder"] = "encoder-embedding/1"
    world.spec_path.write_text(json.dumps(spec))
    with pytest.raises(r.Failure, match="unknown gallery builder"):
        r.runtime_spec()


def test_rootsift_rows_and_points():
    pytest.importorskip("cv2")
    rng = np.random.default_rng(7)
    image = rng.integers(0, 256, (36, 27, 3), dtype=np.uint8).repeat(8, 0).repeat(8, 1)
    rows, points, owners, version = r.sift_features([image, image[:, :, ::-1].copy()])
    assert rows.dtype == np.float32 and rows.shape[1] == 128 and len(rows) == len(points) == len(owners) > 0
    assert np.allclose((rows.astype(np.float64) ** 2).sum(axis=1), 1, atol=1e-4)
    assert set(owners.tolist()) <= {0, 1} and (points >= 0).all() and (points[:, 0] < 216).all() and (points[:, 1] < 288).all()
    assert isinstance(version, str) and version


def test_repository_runtime_pins_are_valid():
    spec = r.runtime_spec()
    assert spec["galleries"]["gallery"]["builder"] in r.BUILDERS
    assert all(item.count("==") == 1 for item in r.requirements(spec))
    assert SimpleNamespace(**r.runtime_records(spec)["opencv/opencv_js.wasm"]).contentType == "application/wasm"
