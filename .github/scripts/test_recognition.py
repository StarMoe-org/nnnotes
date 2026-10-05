"""Recognition bundle workflow: catalog, reference embeddings, ordered create-only publication and the pointer, on
synthetic data."""
import io
import json
import sys
from pathlib import Path

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
    return {"_id": card_id, "_assetID": card_id, "_characterID": card_id, "_rarity": rarity, "_cardType": 1,
            "_memberCardLevelGroup": 1}


def snap(card_id, rank_group=1):
    return {"_id": card_id, "_assetID": card_id, "_characterIDs": [1, card_id], "_rarity": 10, "_cardType": 2,
            "_supportCardLevelGroup": 1, "_supportCardRankGroup": rank_group}


def growth():
    return {"MasterMemberCardLevel": [{"_group": 1, "_level": level} for level in range(1, 81)],
            "MasterMemberCardLevelLimit": [{"_rarity": 2, "_awakeCount": 1, "_limitLevel": 30},
                                           {"_rarity": 2, "_awakeCount": 2, "_limitLevel": 50},
                                           {"_rarity": 3, "_awakeCount": 1, "_limitLevel": 90}],
            "MasterSupportCardLevel": [{"_group": 1, "_level": level} for level in range(1, 61)],
            "MasterSupportCardRank": [{"_group": 1, "_rank": 1, "_limitLevel": 20}, {"_group": 1, "_rank": 5, "_limitLevel": 40},
                                      {"_group": 2, "_rank": 1, "_limitLevel": 70}]}


def normalized(batch):
    """A stand-in encoder: per-channel means and a constant, as unit rows."""
    rows = np.concatenate([batch.mean(axis=(2, 3)), np.ones((len(batch), 1), np.float32)], axis=1)
    return (rows / np.linalg.norm(rows, axis=1, keepdims=True)).astype(np.float32)


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
        spec = json.loads(r.RUNTIME_SPEC.read_text(encoding="utf-8"))
        for index, (name, record) in enumerate(spec["files"].items()):
            raw = f"runtime {name} {index}".encode()
            self.runtime[name] = raw
            spec["files"][name] = {"sha256": r.digest(raw), "bytes": len(raw), "ext": record["ext"]}
        spec["galleries"]["gallery"]["requirements"] = []
        self.spec_path = tmp_path / "recognition-runtime.json"
        self.spec_path.write_text(json.dumps(spec))
        self.models = tmp_path / "models"
        self.models.mkdir()
        for name in r.builder_inputs(spec):
            (self.models / name.rsplit("/", 1)[1]).write_bytes(self.runtime[name])

    def put_runtime(self, names=None):
        for name, raw in self.runtime.items():
            if names is None or name in names:
                record = r.runtime_records(r.runtime_spec(self.spec_path))[name]
                self.store[record["path"]] = (raw, {"content-type": record["contentType"]})

    def tables(self, region):
        return {**{r.KINDS[kind]["table"]: rows for kind, rows in self.masters[region].items()}, **growth()}

    def index(self):
        regions = {}
        for region in self.masters:
            files = {f"{table}.json": r.digest(self.table(region, table)) for table in self.tables(region)}
            regions[region] = {"path": f"/{region.split('-')[0]}/master/", "entry": {"version": f"v-{region}"}, "files": files}
        return json.dumps({"schema_version": 1, "regions": regions}).encode()

    def table(self, region, table):
        return json.dumps({"_allData": self.tables(region)[table]}).encode()

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
                for table in self.tables(region):
                    if url == f"{MASTER}/{region.split('-')[0]}/master/{table}.json":
                        return respond(self.table(region, table))
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
    monkeypatch.setattr(r, "run_encoder", lambda model, batch: normalized(batch))
    w.put_runtime()
    return w


def build(world, name="out", runtime_dir=None):
    out = world.tmp / name
    r.cmd_build(out, world.models, runtime_dir)
    return out


def bundle_of(out):
    pointer = json.loads((out / "site" / r.POINTER).read_text())
    return pointer, json.loads((out / "site" / f"assets/{pointer['bundle']['sha256']}.json").read_text())


def manifest(out, bundle, entry):
    return json.loads((out / "site" / bundle["files"][bundle["entries"][entry]]["path"]).read_text())


def test_canonical_json_writes_integral_numbers_as_javascript_does():
    assert r.canonical({"b": 1, "a": [0.0, 384.0, 47.5, "x"]}) == b'{"a":[0,384,47.5,"x"],"b":1}'
    assert r.document({"box": [0.0, 1.25]}) == b'{\n  "box": [\n    0,\n    1.25\n  ]\n}\n'


def test_catalog_is_the_union_with_the_first_regions_identity_and_level_limit(world):
    world.masters["jp"]["member"][1] = member(2, rarity=3)
    world.masters["jp"]["snap"][1] = snap(2, rank_group=2)
    cards, warnings = r.catalog(r.master_sources(r.regions()))
    assert [(c["kind"], c["id"], c["source"]) for c in cards] == [
        ("member", "1", "hk-tw-mo"), ("member", "2", "hk-tw-mo"), ("snap", "1", "hk-tw-mo"), ("snap", "2", "jp")]
    assert cards[0]["regions"] == ["hk-tw-mo", "jp"] and cards[1]["regions"] == ["hk-tw-mo"]
    assert cards[3]["identity"] == {"assetId": "2", "characterIds": ["1", "2"], "rarity": 10, "cardType": 2}
    assert warnings == ["member:2: identity in jp differs from hk-tw-mo"]
    # Member: the highest training limit of its rarity; Snap: of its rank group, capped by the level curve (60).
    assert [c["levelLimit"] for c in cards] == [50, 50, 40, 60]


def test_level_limit_without_limit_rows_is_the_curve_and_a_missing_curve_stops(world):
    tables = growth()
    tables["MasterMemberCardLevelLimit"] = []
    assert r.level_limit("member", member(1, rarity=4), tables) == 80
    with pytest.raises(r.Failure, match="no level curve"):
        r.level_limit("snap", {**snap(1), "_supportCardLevelGroup": 9}, tables)


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


def test_fit_box_is_imageops_fit():
    image = Image.fromarray(np.random.default_rng(1).integers(0, 256, (500, 300, 3), dtype=np.uint8))
    for source in (image, image.transpose(Image.Transpose.TRANSPOSE), image.resize((384, 384))):
        box = r.fit_box(*source.size, (212, 282))
        explicit = source.resize((212, 282), Image.Resampling.LANCZOS, box=tuple(box))
        assert np.array_equal(np.asarray(explicit), np.asarray(ImageOps.fit(source, (212, 282), Image.Resampling.LANCZOS)))


def test_reference_inputs_fill_the_artwork_window_of_each_kind():
    spec = r.runtime_spec()
    rng = np.random.default_rng(3)
    square = Image.fromarray(rng.integers(0, 256, (384, 384, 3), dtype=np.uint8))
    tensor, steps = r.reference_input(spec, "member", square)
    fitted = ImageOps.fit(square, (212, 282), Image.Resampling.LANCZOS).resize((128, 160), Image.Resampling.BILINEAR)
    assert tensor.shape == (3, 160, 128) and tensor.dtype == np.float32
    assert np.array_equal(tensor, np.asarray(fitted, np.float32).transpose(2, 0, 1) / 255.)
    assert steps[0]["size"] == [212, 282] and steps[1] == {"size": [128, 160], "filter": "bilinear"}
    wide = Image.fromarray(rng.integers(0, 256, (288, 512, 3), dtype=np.uint8))
    tensor, steps = r.reference_input(spec, "snap", wide)
    left, top, right, bottom = steps[0]["box"]
    assert tensor.shape == (3, 128, 224) and (left, right) == (0, 512)
    assert abs((right - left) / (bottom - top) - 314 / 172) < 1e-9 and abs(top - (288 - bottom)) < 1e-9


def test_build_writes_a_closed_content_addressed_bundle(world):
    out = build(world)
    pointer, bundle = bundle_of(out)
    site = out / "site"
    assert pointer["format"] == "moenotes.recognition-pointer/1" and bundle["previous"] is None
    assert bundle["format"] == r.BUNDLE_FORMAT and bundle["counts"] == {"member": 2, "snap": 2}
    assert bundle["entries"] == {"gallery": "gallery/manifest.json", "models": "models/manifest.json"}
    gallery = manifest(out, bundle, "gallery")
    claimed = gallery.pop("galleryId")
    assert r.digest(r.canonical(gallery)) == claimed and gallery["builder"] == "encoder-embed/1"
    assert [f"{c['kind']}:{c['id']}" for c in gallery["cards"]] == ["member:1", "member:2", "snap:1", "snap:2"]
    assert [c["levelLimit"] for c in gallery["cards"]] == [50, 50, 40, 40]
    assert gallery["cards"][3]["art"]["assetPath"] == "jp/ja/SupportCard/2/snap_thumbnail/snap_thumbnail.webp"
    assert gallery["cards"][0]["art"]["reference"][0]["box"] == [47.65957446808511, 0, 336.3404255319149, 384]
    models = manifest(out, bundle, "models")
    assert models["format"] == r.MODELS_FORMAT and set(models["runtime"]) == {"glue", "module", "wasm"}
    assert models["encoders"]["snap"]["similarity"] == 0.81 and models["ranks"]["member"]["center"] == [194.5, 267.2]
    for kind, rows in (("member", [0, 1]), ("snap", [2, 3])):
        section = gallery["embeddings"][kind]
        assert section["cards"] == rows and section["buffer"]["shape"] == [2, 4] and section["dimension"] == 4
        assert section["encoder"] == models["encoders"][kind]["model"]
        vectors = np.frombuffer((site / "assets" / section["buffer"]["file"]).read_bytes(), "<f4").reshape(2, 4)
        assert np.allclose(np.linalg.norm(vectors, axis=1), 1)
    assert not any(name.startswith("art/") for name in bundle["files"])
    for name, record in bundle["files"].items():
        assert record["path"] == f"assets/{record['sha256']}.{record['path'].rsplit('.', 1)[1]}"
        assert record["contentType"] == r.TYPES[record["path"].rsplit(".", 1)[1]]
        assert (site / record["path"]).is_file() != (name in r.runtime_records(r.runtime_spec()))
    report = json.loads((out / "report.json").read_text())
    assert report["bundle"] == pointer["bundle"] and report["gallery"]["rows"] == {"member": 2, "snap": 2}
    r.local_closure(out, r.runtime_spec())


def test_the_builder_needs_the_pinned_encoder_models(world):
    (world.models / "member-encoder.onnx").write_bytes(b"other bytes")
    with pytest.raises(r.Failure, match="no file matching the pin of models/member-encoder.onnx"):
        build(world)


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


def test_embeddings_must_be_unit_vectors(world, monkeypatch):
    monkeypatch.setattr(r, "run_encoder", lambda model, batch: 2 * normalized(batch))
    with pytest.raises(r.Failure, match="not unit vectors"):
        build(world)


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
    assert set(keys[:-4]) == {bundle["files"][f"gallery/{kind}.f32.bin"]["path"] for kind in r.KINDS}
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
    record = bundle["files"]["gallery/member.f32.bin"]
    world.store[record["path"]] = ((out / "site" / record["path"]).read_bytes(), {"content-type": record["contentType"]})
    r.cmd_publish(out, dry_run=False)
    assert record["path"] not in {put["Key"] for put in world.puts}
    world.puts.clear()
    r.cmd_publish(out, dry_run=False)          # again: everything is there and the pointer already names it
    assert world.puts == []


def test_an_existing_object_with_other_bytes_or_type_stops_before_any_write(world, monkeypatch):
    out = build(world)
    _, bundle = bundle_of(out)
    record = bundle["files"]["gallery/snap.f32.bin"]
    world.store[record["path"]] = (b"other", {"content-type": "application/octet-stream"})
    monkeypatch.setattr(r, "s3_client", lambda: pytest.fail("no write identity before the checks pass"))
    with pytest.raises(r.Failure, match="bytes differ"):
        r.cmd_publish(out, dry_run=False)
    world.store[record["path"]] = ((out / "site" / record["path"]).read_bytes(), {"content-type": "image/webp"})
    with pytest.raises(r.Failure, match="Content-Type"):
        r.cmd_publish(out, dry_run=False)
    assert world.puts == []


def test_missing_runtime_files_stop_publication_but_not_a_dry_run(world, monkeypatch):
    out = build(world)
    world.store.clear()
    monkeypatch.setattr(r, "s3_client", lambda: pytest.fail("a dry run gets no write identity"))
    r.cmd_publish(out, dry_run=True)
    report = json.loads((out / "publication.json").read_text())
    assert len(report["unavailableRuntime"]) == len(world.runtime) and report["counts"]["existing"] == 0
    with pytest.raises(r.Failure, match="pinned file"):
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


def published_v1(world, cards):
    """A bundle of the previous recipe in the bucket: its gallery names the cards it covered."""
    gallery = json.dumps({"format": "ournotes.browser-feature-gallery/3", "cards": cards}).encode()
    gallery_record = {"path": f"assets/{r.digest(gallery)}.json", "sha256": r.digest(gallery), "bytes": len(gallery),
                      "contentType": "application/json"}
    bundle = json.dumps({"format": "moenotes.recognition-bundle/1", "inputsSha256": "0" * 64, "previous": None,
                         "entries": {"gallery": "gallery/manifest.json"}, "files": {"gallery/manifest.json": gallery_record}}).encode()
    for raw in (gallery, bundle):
        world.store[f"assets/{r.digest(raw)}.json"] = (raw, {"content-type": "application/json"})
    world.store[r.POINTER] = (r.pointer_document(r.digest(bundle), len(bundle)), {"content-type": "application/json"})
    return {"sha256": r.digest(bundle), "bytes": len(bundle)}


def test_a_bundle_of_the_previous_recipe_is_followed_and_its_cards_kept(world, monkeypatch):
    previous = published_v1(world, [{"kind": "member", "id": "1"}, {"kind": "snap", "id": "2"}])
    outputs = {}
    monkeypatch.setattr(r, "output", outputs.__setitem__)
    r.cmd_plan()
    assert outputs["build"] == "true"
    out = build(world)
    assert bundle_of(out)[1]["previous"] == previous
    r.cmd_publish(out, dry_run=False)
    assert json.loads(world.store[r.POINTER][0])["bundle"] == bundle_of(out)[0]["bundle"]
    published_v1(world, [{"kind": "snap", "id": "3"}])
    with pytest.raises(r.Failure, match="lacks published card"):
        r.cmd_plan()


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


def test_a_threshold_or_level_limit_change_is_a_new_input(world, monkeypatch):
    outputs = {}
    monkeypatch.setattr(r, "output", outputs.__setitem__)
    r.cmd_publish(build(world), dry_run=False)
    spec = json.loads(world.spec_path.read_text())
    original = spec["pipeline"]["fields"]["member"]["confidence"]
    spec["pipeline"]["fields"]["member"]["confidence"] = 0.999
    world.spec_path.write_text(json.dumps(spec))
    r.cmd_plan()
    assert outputs["build"] == "true"
    spec["pipeline"]["fields"]["member"]["confidence"] = original
    world.spec_path.write_text(json.dumps(spec))
    r.cmd_plan()
    assert outputs["build"] == "false"
    world.masters["hk-tw-mo"]["snap"][0] = snap(1, rank_group=2)
    r.cmd_plan()
    assert outputs["build"] == "true"


def test_runtime_job_uploads_missing_pins_and_stages_the_encoder_models(world, monkeypatch):
    spec = r.runtime_spec()
    missing = ["models/locator.onnx", "models/snap-encoder.onnx"]
    for name in missing:
        del world.store[r.runtime_records(spec)[name]["path"]]
    assets = [{"id": index, "name": f"{record['sha256']}.{record['ext']}", "size": record["bytes"], "state": "uploaded"}
              for index, record in enumerate(spec["files"].values())]
    monkeypatch.setattr(r, "gh_json_lines", lambda args: [{"id": 1, "draft": True, "assets": assets}])
    by_id = {index: world.runtime[name] for index, name in enumerate(spec["files"])}
    monkeypatch.setattr(r, "gh_download", lambda repository, asset_id, target: target.write_bytes(by_id[asset_id]))
    r.cmd_runtime(world.tmp / "work-dry", world.tmp / "dry.json", world.tmp / "stage-dry", dry_run=True)
    assert world.puts == []
    assert sorted(p.name for p in (world.tmp / "stage-dry").iterdir()) == sorted(
        r.runtime_records(spec)[name]["path"].removeprefix("assets/") for name in r.builder_inputs(spec))
    r.cmd_runtime(world.tmp / "work", world.tmp / "runtime.json", world.tmp / "stage", dry_run=False)
    assert sorted(put["Key"] for put in world.puts) == sorted(r.runtime_records(spec)[name]["path"] for name in missing)
    report = json.loads((world.tmp / "runtime.json").read_text())
    assert report["complete"] and sum(row["state"] == "uploaded" for row in report["objects"]) == 2
    assert {row["name"]: row["from"] for row in report["staged"]} == {"models/member-encoder.onnx": "bucket",
                                                                      "models/snap-encoder.onnx": "release"}
    for name in r.builder_inputs(spec):
        assert r.pinned_file(world.tmp / "stage", name, spec["files"][name])


@pytest.mark.parametrize("change, message", [
    (lambda s: s["galleries"]["gallery"].update(builder="sift-rootsift/1"), "unknown gallery builder"),
    (lambda s: s["files"].update({"models/extra.onnx": {"sha256": "e" * 64, "bytes": 1, "ext": "onnx"}}), "exactly one"),
    (lambda s: s["pipeline"]["encoders"]["snap"].update(similarity=1.5), "encoders.snap.similarity"),
    (lambda s: s["pipeline"]["locator"].update(classes=["snap", "member"]), "locator classes"),
    (lambda s: s["pipeline"]["ranks"].pop("snap"), "ranks per kind"),
    (lambda s: s["runtime"].pop("module"), "glue, module and wasm"),
])
def test_invalid_pins_are_rejected(world, change, message):
    spec = json.loads(world.spec_path.read_text())
    change(spec)
    world.spec_path.write_text(json.dumps(spec))
    with pytest.raises(r.Failure, match=message):
        r.runtime_spec()


def protobuf_varint(value):
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def protobuf_field(number, payload):
    if isinstance(payload, int):
        return protobuf_varint(number << 3) + protobuf_varint(payload)
    payload = payload.encode() if isinstance(payload, str) else payload
    return protobuf_varint(number << 3 | 2) + protobuf_varint(len(payload)) + payload


def pooling_encoder():
    """A three-node ONNX model, image [N, 3, H, W] -> embedding [N, 3]: channel means, L2-normalized."""
    def value(name, dims):
        shape = b"".join(protobuf_field(1, protobuf_field(2, d) if isinstance(d, str) else protobuf_field(1, d)) for d in dims)
        return protobuf_field(1, name) + protobuf_field(2, protobuf_field(1, protobuf_field(1, 1) + protobuf_field(2, shape)))

    def node(op, inputs, outputs):
        return b"".join(protobuf_field(1, i) for i in inputs) + b"".join(protobuf_field(2, o) for o in outputs) + protobuf_field(4, op)

    graph = (protobuf_field(1, node("GlobalAveragePool", ["image"], ["pooled"])) + protobuf_field(1, node("Flatten", ["pooled"], ["flat"]))
             + protobuf_field(1, node("LpNormalization", ["flat"], ["embedding"])) + protobuf_field(2, "pooling")
             + protobuf_field(11, value("image", ["N", 3, "H", "W"])) + protobuf_field(12, value("embedding", ["N", 3])))
    return protobuf_field(1, 8) + protobuf_field(7, graph) + protobuf_field(8, protobuf_field(1, "") + protobuf_field(2, 17))


def test_onnx_runtime_embeds_a_batch(tmp_path):
    pytest.importorskip("onnxruntime")
    model = tmp_path / "encoder.onnx"
    model.write_bytes(pooling_encoder())
    batch = np.random.default_rng(5).random((5, 3, 16, 12), dtype=np.float32)
    vectors = r.run_encoder(model, batch)
    means = batch.mean(axis=(2, 3))
    assert vectors.shape == (5, 3) and np.allclose(vectors, means / np.linalg.norm(means, axis=1, keepdims=True), atol=1e-6)


def test_repository_runtime_pins_are_valid():
    spec = r.runtime_spec()
    assert spec["galleries"]["gallery"]["builder"] in r.BUILDERS
    assert all(item.count("==") == 1 for item in r.requirements(spec))
    assert r.runtime_records(spec)["runtime/ort-wasm-simd-threaded.wasm"]["contentType"] == "application/wasm"
    assert r.builder_inputs(spec) == ["models/member-encoder.onnx", "models/snap-encoder.onnx"]
