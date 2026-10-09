#!/usr/bin/env python3
"""The screenshot recognition bundle (.github/workflows/recognition.yml, .github/RECOGNITION.md).

MoeNotes' card box recognizes Member and Snap cards in screenshots in the browser. Everything its Worker loads lives
in the story site's bucket, content-addressed (`assets/<sha256>.<ext>`); one small mutable pointer,
`recognition/current.json`, names the current bundle manifest by its SHA-256 and size.

    plan                  the card catalog (every enabled region's master data) and its artwork against the published
                          bundle: GitHub outputs `build` (true: the catalog, artwork, runtime pins or recipe changed, or
                          $FORCE) and `runtime` (true: a pinned runtime file is not in the bucket yet)
    runtime --stage DIR [--dry-run]
                          the pinned files the bucket lacks, downloaded from this repository's draft release (`release`
                          of recognition-runtime.json, `gh api`), checked and uploaded; the encoder models the gallery
                          builder runs are written to DIR (from the bucket, or the release while they are missing)
    build OUT --models DIR [--runtime-dir DIR]
                          the bundle: artwork from the asset service (SHA-256 and size checked), each card's reference
                          embedding (the pinned encoder models in --models, ONNX Runtime on the CPU), the gallery, model
                          and bundle manifests and the new pointer, as the bucket lays them out, under OUT/site;
                          --runtime-dir adds every pinned file for a local preview
    publish OUT [--dry-run]
                          every object of the bundle the bucket lacks (dependencies, then manifests, then the bundle
                          manifest), each read back over public HTTP, then the pointer; never deletes or overwrites
    requirements          the pinned gallery builder libraries (for pip)

Bucket: $STORY_S3_ENDPOINT, $STORY_S3_BUCKET, $RECOGNITION_S3_PREFIX (a key prefix, may be empty), credentials
$STORY_S3_ACCESS_KEY / $STORY_S3_SECRET_KEY (publish and runtime only). Sources: $MASTERDATA_BASE_URL (decoded master
data, index.json with every file's SHA-256), $RECOGNITION_REGIONS, $RECOGNITION_ASSET_API (the asset service). plan and
build read the catalog once the asset service has exported every region's master version, waiting up to
$RECOGNITION_EXPORT_WAIT seconds (3600) and checking every $RECOGNITION_EXPORT_POLL seconds (60).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from http_compression import decode_content  # noqa: E402
from story_site import env, output, summary  # noqa: E402

RUNTIME_SPEC = HERE.parent / "recognition-runtime.json"
SPEC_FORMAT = "moenotes.recognition-runtime/2"
RECIPE = "moenotes.recognition-gallery/2"
BUNDLE_FORMAT = "moenotes.recognition-bundle/2"
GALLERY_FORMAT = "moenotes.embedding-gallery/1"
MODELS_FORMAT = "moenotes.recognition-models/1"
# Bundles any recipe published; the newest names the cards every later gallery keeps.
PUBLISHED_FORMATS = ("moenotes.recognition-bundle/1", BUNDLE_FORMAT)
POINTER = "recognition/current.json"
ASSET_CACHE = "public, max-age=31536000, immutable"      # content-addressed: never changes
POINTER_CACHE = "no-cache"
ORIGIN = "https://bdon.moe"                              # read-back checks CORS as the site's Worker sees it
USER_AGENT = "moenotes-recognition (GitHub Actions)"
TYPES = {"json": "application/json", "js": "text/javascript; charset=utf-8", "mjs": "text/javascript; charset=utf-8",
         "wasm": "application/wasm", "onnx": "application/octet-stream", "bin": "application/octet-stream",
         "webp": "image/webp", "png": "image/png"}
# The asset service publishes each server's files under /{region}/{language}/.
ASSET_REGIONS = {"hk-tw-mo": "tw/zh-Hans", "en": "en/en", "kr": "kr/ko", "jp": "jp/ja"}
KINDS = {"member": {"table": "MasterMemberCard", "directory": "MemberCard", "label": "member_thumbnail", "file": "square.webp",
                    "levels": "MasterMemberCardLevel", "limits": "MasterMemberCardLevelLimit"},
         "snap": {"table": "MasterSupportCard", "directory": "SupportCard", "label": "snap_thumbnail", "file": "snap_thumbnail.webp",
                  "levels": "MasterSupportCardLevel", "limits": "MasterSupportCardRank"}}
GROWTH_TABLES = tuple(info[key] for info in KINDS.values() for key in ("levels", "limits"))
# Asset service release states whose files are published (/versions/current_version.json).
EXPORT_STATES = ("succeeded", "partial")
RUNTIME_ROLES = ("glue", "module", "wasm")
STAGES = ("encoders", "fields", "ranks")
HEX64 = re.compile(r"[a-f0-9]{64}")
ASSET_PATH = re.compile(r"assets/([a-f0-9]{64})\.([a-z0-9]+)")
SCOPE = ("Gallery of reference embeddings for {member} Member and {snap} Snap cards: a card is identified when the cosine "
         "similarity of its artwork window to the nearest same-kind reference reaches the encoder's `similarity` and "
         "exceeds the second nearest by `margin`; otherwise it stays unknown. IDs are exact decimal strings. Files are "
         "relative to this manifest.")


class Failure(Exception):
    """A checked condition that stops the run (the message is safe to print)."""


def require(condition, reason: str) -> None:
    if not condition:
        raise Failure(reason)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- JSON
def js_numbers(value):
    """Integral floats as integers, as JavaScript's JSON.stringify writes them (the Worker hashes canonical JSON)."""
    if isinstance(value, float):
        require(math.isfinite(value), "non-finite number in a manifest")
        return int(value) if value.is_integer() else value
    if isinstance(value, dict):
        return {key: js_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [js_numbers(item) for item in value]
    return value


def canonical(value) -> bytes:
    return json.dumps(js_numbers(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def document(value) -> bytes:
    return (json.dumps(js_numbers(value), ensure_ascii=False, indent=2) + "\n").encode()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- HTTP
@dataclass
class Response:
    status: int
    headers: dict
    body: bytes


def fetch(url: str, *, origin: bool = False, missing_ok: bool = False, decode: bool = True,
          timeout: int = 120) -> Response | None:
    """One GET with retries on transient errors; header names lower-cased."""
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Cache-Control": "no-cache"}
    if origin:
        headers["Origin"] = ORIGIN
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                fields = {key.lower(): value for key, value in response.headers.items()}
                body = response.read()
                if decode:
                    body = decode_content(body, fields.get("content-encoding"))
                return Response(response.status, fields, body)
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise Failure(f"HTTP {error.code}: {url}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == 3:
                raise Failure(f"transport failed ({type(error).__name__}): {url}") from None
        time.sleep(1 + 2 * attempt)
    raise Failure(f"unreachable: {url}")


def site_base() -> str:
    prefix = os.environ.get("RECOGNITION_S3_PREFIX", "").strip("/")
    return (f"{env('STORY_S3_ENDPOINT', 'https://storage.bdon.moe').rstrip('/')}/"
            f"{env('STORY_S3_BUCKET', 'moenotes')}/" + (f"{prefix}/" if prefix else ""))


def object_key(path: str) -> str:
    prefix = os.environ.get("RECOGNITION_S3_PREFIX", "").strip("/")
    return (f"{prefix}/" if prefix else "") + path


def media_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def verify_public(record: dict, *, missing_ok: bool = False, keep: dict | None = None) -> dict | None:
    """The object as the browser gets it: exact bytes, Content-Type, no transfer encoding, CORS. `keep` receives the
    checked bytes under the record's path."""
    response = fetch(site_base() + record["path"], origin=True, missing_ok=missing_ok, decode=False)
    if response is None:
        return None
    problems = []
    encoding = response.headers.get("content-encoding")
    if encoding and encoding.lower() != "identity":
        problems.append(f"Content-Encoding {encoding}")
    if len(response.body) != record["bytes"] or digest(response.body) != record["sha256"]:
        problems.append("bytes differ")
    if media_type(response.headers.get("content-type")) != media_type(record["contentType"]):
        problems.append(f"Content-Type {response.headers.get('content-type')!r}")
    if response.headers.get("access-control-allow-origin") not in ("*", ORIGIN):
        problems.append("no Access-Control-Allow-Origin")
    require(not problems, f"{record['path']}: {', '.join(problems)}")
    if keep is not None:
        keep[record["path"]] = response.body
    return {"contentType": response.headers.get("content-type"), "cacheControl": response.headers.get("cache-control"),
            "accessControlAllowOrigin": response.headers.get("access-control-allow-origin")}


def object_present(record: dict) -> bool:
    """HEAD by the object's URL (bucket listings are cached without their query parameters)."""
    request = urllib.request.Request(site_base() + record["path"], method="HEAD",
                                     headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return int(response.headers.get("Content-Length", -1)) == record["bytes"]
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise Failure(f"HTTP {error.code}: HEAD {record['path']}") from None


# ---------------------------------------------------------------- runtime pins
def number(value, what: str, low: float | None = None, high: float | None = None, integer: bool = False):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    ok = ok and (not integer or isinstance(value, int)) and (low is None or value >= low) and (high is None or value <= high)
    require(ok, f"recognition-runtime.json: {what} is invalid")
    return value


def pair(value, what: str, low: float | None = None, integer: bool = False) -> list:
    require(isinstance(value, list) and len(value) == 2, f"recognition-runtime.json: {what} is invalid")
    return [number(item, what, low, integer=integer) for item in value]


def check_pipeline(spec: dict) -> set[str]:
    """The model stages and their parameters; returns the pinned files they name."""
    files, used = spec["files"], []

    def model(stage: dict, what: str) -> None:
        name = stage.get("model")
        require(name in files and files[name]["ext"] == "onnx", f"recognition-runtime.json: {what} model is not a pinned .onnx file")
        used.append(name)

    runtime = spec.get("runtime")
    require(isinstance(runtime, dict) and set(runtime) == set(RUNTIME_ROLES) and all(runtime[r] in files for r in RUNTIME_ROLES),
            "recognition-runtime.json: runtime names its glue, module and wasm files")
    used.extend(runtime.values())
    pipeline = spec.get("pipeline")
    require(isinstance(pipeline, dict) and set(pipeline) == {"tiles", "locator", *STAGES},
            "recognition-runtime.json: pipeline has tiles, locator, encoders, fields and ranks")
    require(isinstance(pipeline["tiles"], dict) and set(pipeline["tiles"]) == set(KINDS), "recognition-runtime.json: tiles per kind")
    for kind, size in pipeline["tiles"].items():
        pair(size, f"tiles.{kind}", 1)
    locator = pipeline["locator"]
    model(locator, "locator")
    require(locator.get("classes") == list(KINDS), "recognition-runtime.json: locator classes are the card kinds in order")
    for key in ("longEdge", "multiple", "stride", "maxPeaks", "maxDetections"):
        number(locator.get(key), f"locator.{key}", 1, integer=True)
    number(locator.get("pad"), "locator.pad", 0, 255, integer=True)
    for key in ("threshold", "overlap", "minVisible"):
        number(locator.get(key), f"locator.{key}", 0, 1)
    for stage in STAGES:
        require(isinstance(pipeline[stage], dict) and set(pipeline[stage]) == set(KINDS), f"recognition-runtime.json: {stage} per kind")
        for kind, item in pipeline[stage].items():
            what = f"{stage}.{kind}"
            model(item, what)
            pair(item.get("input"), f"{what}.input", 1, integer=True)
            if stage == "encoders":
                number(item.get("inset"), f"{what}.inset", 0)
                number(item.get("similarity"), f"{what}.similarity", -1, 1)
                number(item.get("margin"), f"{what}.margin", 0, 2)
                continue
            number(item.get("classes"), f"{what}.classes", 2, integer=True)
            number(item.get("edge"), f"{what}.edge", 0)
            number(item.get("confidence"), f"{what}.confidence", 0, 1)
            number(item.get("margin"), f"{what}.margin", 0, 1)
            if stage == "fields":
                for key in ("left", "bottom", "width", "height"):
                    number(item.get(key), f"{what}.{key}")
            else:
                pair(item.get("center"), f"{what}.center")
                number(item.get("size"), f"{what}.size", 1)
                pair(item.get("icon"), f"{what}.icon", 1)
    require(len(used) == len(set(used)) and set(used) == set(files),
            "recognition-runtime.json: every pinned file is used by exactly one runtime role or model")
    return set(used)


def runtime_spec(path: Path | None = None) -> dict:
    spec = json.loads((path or RUNTIME_SPEC).read_text(encoding="utf-8"))
    require(spec.get("format") == SPEC_FORMAT, "recognition-runtime.json: unknown format")
    require(isinstance(spec.get("release"), str) and re.fullmatch(r"[A-Za-z0-9._-]+", spec["release"]),
            "recognition-runtime.json: invalid release tag")
    require(isinstance(spec.get("source"), str) and spec["source"].startswith("https://"), "recognition-runtime.json: source")
    require(isinstance(spec.get("files"), dict) and spec["files"], "recognition-runtime.json: no files")
    for name, record in spec["files"].items():
        require(re.fullmatch(r"[a-z]+/[A-Za-z0-9._-]+", name) and HEX64.fullmatch(record.get("sha256", ""))
                and isinstance(record.get("bytes"), int) and record["bytes"] > 0 and record.get("ext") in TYPES
                and name.endswith("." + record["ext"]), f"recognition-runtime.json: invalid record {name}")
    check_pipeline(spec)
    for name, gallery in spec.get("galleries", {}).items():
        require(re.fullmatch(r"[a-z][a-z0-9-]*", name) and gallery.get("builder") in BUILDERS,
                f"recognition-runtime.json: unknown gallery builder {gallery.get('builder')!r} for {name!r}")
    require(list(spec.get("galleries", {})) == ["gallery"], "recognition-runtime.json: the bundle has one gallery, 'gallery'")
    return spec


def runtime_records(spec: dict) -> dict:
    return {name: {"path": f"assets/{record['sha256']}.{record['ext']}", "sha256": record["sha256"],
                   "bytes": record["bytes"], "contentType": TYPES[record["ext"]]}
            for name, record in spec["files"].items()}


def builder_inputs(spec: dict) -> list[str]:
    """The pinned files the gallery builder runs: the encoder model of each kind."""
    return [spec["pipeline"]["encoders"][kind]["model"] for kind in KINDS]


def requirements(spec: dict) -> list[str]:
    return [item for gallery in spec["galleries"].values() for item in gallery.get("requirements", [])]


def check_requirements(spec: dict) -> None:
    """Embeddings depend on the library versions: build only with the pinned ones."""
    from importlib.metadata import PackageNotFoundError, version
    for item in requirements(spec):
        name, _, wanted = item.partition("==")
        try:
            installed = version(name)
        except PackageNotFoundError:
            installed = None
        require(installed == wanted, f"{name} {installed or 'missing'}; the gallery recipe pins {wanted}")


def pinned_file(directory: Path, name: str, record: dict) -> Path:
    """The file in `directory` (any depth, named after the pin or `<sha256>.<ext>`) whose bytes match the pin."""
    wanted = {name.rsplit("/", 1)[1], f"{record['sha256']}.{record['ext']}"}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name in wanted:
            raw = path.read_bytes()
            if len(raw) == record["bytes"] and digest(raw) == record["sha256"]:
                return path
    raise Failure(f"{directory} has no file matching the pin of {name}")


# ---------------------------------------------------------------- the card catalog
def regions() -> list[str]:
    names = list(dict.fromkeys(re.split(r"[\s,]+", env("RECOGNITION_REGIONS", "hk-tw-mo en kr jp").strip())))
    names = [name for name in names if name]
    unknown = [name for name in names if name not in ASSET_REGIONS]
    require(names and not unknown, f"RECOGNITION_REGIONS: unknown region(s) {', '.join(unknown) or '(none)'}")
    return names


def decimal(value, what: str) -> str:
    require(isinstance(value, int) and not isinstance(value, bool) and value >= 1, f"{what}: not a positive integer")
    return str(value)


def identity(kind: str, row: dict) -> dict:
    characters = [row.get("_characterID")] if kind == "member" else row.get("_characterIDs")
    require(isinstance(characters, list) and characters, f"{kind} {row.get('_id')}: character IDs missing")
    for field in ("_rarity", "_cardType"):
        require(isinstance(row.get(field), int) and not isinstance(row.get(field), bool), f"{kind} {row.get('_id')}: {field}")
    return {"assetId": decimal(row.get("_assetID"), f"{kind} {row.get('_id')} asset"),
            "characterIds": [decimal(c, f"{kind} {row.get('_id')} character") for c in characters],
            "rarity": row["_rarity"], "cardType": row["_cardType"]}


def integer_field(row: dict, field: str, what: str) -> int:
    value = row.get(field)
    require(isinstance(value, int) and not isinstance(value, bool), f"{what}: {field}")
    return value


def level_limit(kind: str, row: dict, growth: dict) -> int:
    """The highest level the card reaches: its level curve, capped by the highest training (Member, by rarity) or
    limit-break (Snap, by rank group) level limit."""
    what = f"{kind} {row.get('_id')}"
    group = integer_field(row, "_memberCardLevelGroup" if kind == "member" else "_supportCardLevelGroup", what)
    curve = max((integer_field(r, "_level", what) for r in growth[KINDS[kind]["levels"]]
                 if integer_field(r, "_group", what) == group), default=0)
    require(curve >= 1, f"{what}: no level curve")
    if kind == "member":
        rarity = row["_rarity"]
        limits = [integer_field(r, "_limitLevel", what) for r in growth[KINDS[kind]["limits"]] if r.get("_rarity") == rarity]
    else:
        rank_group = integer_field(row, "_supportCardRankGroup", what)
        limits = [integer_field(r, "_limitLevel", what) for r in growth[KINDS[kind]["limits"]] if r.get("_group") == rank_group]
    return min(max(limits), curve) if limits else curve


def master_table(base: str, region: str, entry: dict, table: str) -> tuple[bytes, list]:
    name = table + ".json"
    expected = (entry.get("files") or {}).get(name)
    require(isinstance(expected, str) and HEX64.fullmatch(expected), f"{region}: index.json lists no {name}")
    for _ in range(3):
        raw = fetch(base + entry["path"] + name).body
        if digest(raw) == expected:
            break
    else:
        raise Failure(f"{region} {name}: SHA-256 differs from index.json (the service changed snapshots? run again)")
    data = json.loads(raw)
    require(isinstance(data.get("_allData"), list), f"{region} {name}: no _allData")
    return raw, data["_allData"]


def master_sources(names: list[str]) -> list[dict]:
    """Each region's card and level tables from moenotes-masterdata-sync, SHA-256 checked against index.json."""
    base = env("MASTERDATA_BASE_URL", "https://metadata.bdon.moe").rstrip("/")
    index = json.loads(fetch(f"{base}/index.json").body)
    sources = []
    for region in names:
        entry = (index.get("regions") or {}).get(region)
        require(isinstance(entry, dict), f"index.json has no region {region}")
        require(re.fullmatch(r"/([a-z]+/)?master/", entry.get("path", "")), f"{region}: unexpected master path")
        tables, rows, growth = {}, {}, {}
        for table in [info["table"] for info in KINDS.values()] + list(GROWTH_TABLES):
            raw, data = master_table(base, region, entry, table)
            tables[table] = {"sha256": digest(raw), "bytes": len(raw)}
            growth[table] = data
        for kind, info in KINDS.items():
            rows[kind] = {decimal(row.get("_id"), f"{region} {kind} id"): row for row in growth.pop(info["table"])}
        sources.append({"region": region, "masterVersion": (entry.get("entry") or {}).get("version"),
                        "tables": tables, "rows": rows, "growth": growth})
    return sources


def export_lag(api: str, sources: list[dict]) -> list[str]:
    """The regions whose latest asset service release (/versions/current_version.json) is not at the master version the
    catalog was read from: their new cards' artwork is not published yet."""
    status = json.loads(fetch(f"{api}/versions/current_version.json").body)
    releases = {entry.get("metadata_region"): entry for entry in (status.get("regions") or {}).values()
                if isinstance(entry, dict)}
    lag = []
    for source in sources:
        release = releases.get(source["region"]) or {}
        if release.get("master_version") != source["masterVersion"] or release.get("state") not in EXPORT_STATES:
            lag.append(f"{source['region']} (master {source['masterVersion']}, asset release "
                       f"{release.get('master_version')} {release.get('state')})")
    return lag


def exported_sources(names: list[str]) -> list[dict]:
    """master_sources once the asset service has exported each region's master version. Master data moves first, so
    both are read again every $RECOGNITION_EXPORT_POLL seconds for up to $RECOGNITION_EXPORT_WAIT seconds."""
    api = env("RECOGNITION_ASSET_API", "https://assets.bdon.moe").rstrip("/")
    poll = max(1, int(env("RECOGNITION_EXPORT_POLL", "60")))
    attempts = max(0, int(env("RECOGNITION_EXPORT_WAIT", "3600"))) // poll + 1
    for attempt in range(attempts):
        sources = master_sources(names)
        lag = export_lag(api, sources)
        if not lag:
            return sources
        if attempt + 1 < attempts:
            print(f"waiting for the asset service to export {', '.join(lag)}", flush=True)
            time.sleep(poll)
    raise Failure(f"the asset service has not exported {', '.join(lag)}; the pointer stays on the published bundle")


def catalog(sources: list[dict]) -> tuple[list[dict], list[str]]:
    """The union of every region's cards; a card's identity and level limit are the first region's (in
    RECOGNITION_REGIONS order)."""
    cards, warnings = [], []
    for kind in KINDS:
        ids = sorted({card_id for source in sources for card_id in source["rows"][kind]}, key=int)
        for card_id in ids:
            present = [source for source in sources if card_id in source["rows"][kind]]
            row = present[0]["rows"][kind][card_id]
            first = identity(kind, row)
            matching = [s["region"] for s in present if identity(kind, s["rows"][kind][card_id]) == first]
            differing = [s["region"] for s in present if s["region"] not in matching]
            if differing:
                warnings.append(f"{kind}:{card_id}: identity in {', '.join(differing)} differs from {present[0]['region']}")
            cards.append({"kind": kind, "id": card_id, "identity": first, "regions": matching, "source": present[0]["region"],
                          "levelLimit": level_limit(kind, row, present[0]["growth"])})
    require(cards, "the catalog is empty")
    for kind in KINDS:
        require(any(card["kind"] == kind for card in cards), f"the catalog has no {kind} cards")
    return cards, warnings


def art_entry(api: str, card: dict) -> dict:
    """The card's artwork in the asset service listing of its source region."""
    info = KINDS[card["kind"]]
    directory = f"{ASSET_REGIONS[card['source']]}/{info['directory']}/{card['identity']['assetId']}/{info['label']}/"
    listing = json.loads(fetch(f"{api}/{directory}").body)
    path = f"/{directory}{info['file']}"
    matches = [item for item in listing.get("files", []) if isinstance(item, dict) and item.get("path") == path]
    key = f"{card['kind']}:{card['id']}"
    require(matches, f"{key}: the asset service lists no {path}")
    require(len({(m.get("sha256"), m.get("bytes")) for m in matches}) == 1, f"{key}: {path} has differing listings")
    entry = matches[0]
    meta = entry.get("metadata") or {}
    require(HEX64.fullmatch(str(entry.get("sha256"))) and isinstance(entry.get("bytes"), int) and entry["bytes"] > 0
            and re.fullmatch(r"/files/[A-Za-z0-9]+", str(entry.get("file"))), f"{key}: invalid listing for {path}")
    size = [meta.get(n) if isinstance(meta.get(n), int) and meta[n] > 0 else None for n in ("width", "height")]
    return {"assetPath": directory + info["file"], "file": entry["file"], "sha256": entry["sha256"],
            "bytes": entry["bytes"], "width": size[0], "height": size[1]}


def resolve_art(cards: list[dict]) -> list[dict]:
    api = env("RECOGNITION_ASSET_API", "https://assets.bdon.moe").rstrip("/")
    with concurrent.futures.ThreadPoolExecutor(8) as executor:
        return list(executor.map(lambda card: art_entry(api, card), cards))


def inputs_identity(spec: dict, cards: list[dict], arts: list[dict]) -> str:
    """What a bundle is made from: the recipe, the pinned files, model parameters, gallery builder and its libraries,
    every card, its level limit and its artwork."""
    return digest(canonical({
        "recipe": RECIPE, "files": {name: record["sha256"] for name, record in spec["files"].items()},
        "runtime": spec["runtime"], "pipeline": spec["pipeline"], "galleries": spec["galleries"],
        "cards": [{"kind": c["kind"], "id": c["id"], "identity": c["identity"], "regions": c["regions"],
                   "levelLimit": c["levelLimit"], "art": {"sha256": a["sha256"], "assetPath": a["assetPath"]}}
                  for c, a in zip(cards, arts)],
    }))


# ---------------------------------------------------------------- the published bundle
def pointer_document(sha: str, size: int) -> bytes:
    return (json.dumps({"format": "moenotes.recognition-pointer/1", "bundle": {"sha256": sha, "bytes": size}},
                       indent=2) + "\n").encode()


def parse_pointer(raw: bytes) -> dict:
    pointer = json.loads(raw)
    bundle = pointer.get("bundle") or {}
    require(pointer.get("format") == "moenotes.recognition-pointer/1" and HEX64.fullmatch(str(bundle.get("sha256")))
            and isinstance(bundle.get("bytes"), int) and bundle["bytes"] > 0, "the published pointer is invalid")
    return {"sha256": bundle["sha256"], "bytes": bundle["bytes"]}


def read_published(path: str, sha: str, size: int) -> bytes:
    raw = fetch(site_base() + path, origin=True).body
    require(len(raw) == size and digest(raw) == sha, f"{path}: published bytes differ from their reference")
    return raw


def published_state() -> dict | None:
    """The current pointer, bundle manifest and gallery over public HTTP (None: nothing published yet)."""
    response = fetch(site_base() + POINTER, origin=True, missing_ok=True)
    if response is None:
        return None
    pointer = parse_pointer(response.body)
    bundle = json.loads(read_published(f"assets/{pointer['sha256']}.json", pointer["sha256"], pointer["bytes"]))
    require(bundle.get("format") in PUBLISHED_FORMATS, "the published bundle manifest has an unknown format")
    record = bundle["files"][bundle["entries"]["gallery"]]
    gallery = json.loads(read_published(record["path"], record["sha256"], record["bytes"]))
    require(isinstance(gallery.get("cards"), list), "the published gallery has no cards")
    return {"pointer": pointer, "bundle": bundle, "gallery": gallery}


def sort_keys(keys) -> list[str]:
    return sorted(keys, key=lambda k: (k.split(":")[0], int(k.split(":")[1])))


def card_keys(cards) -> set[str]:
    return {f"{card['kind']}:{card['id']}" for card in cards}


def superset_problem(state: dict | None, keys: set[str]) -> str | None:
    """A new gallery keeps every published card: a catalog that loses one is not published."""
    if state is None:
        return None
    lost = sort_keys(card_keys(state["gallery"]["cards"]) - keys)
    return f"the catalog lacks published card(s) {', '.join(lost)}" if lost else None


# ---------------------------------------------------------------- plan
def cmd_plan() -> None:
    spec = runtime_spec()
    sources = exported_sources(regions())
    cards, warnings = catalog(sources)
    arts = resolve_art(cards)
    inputs = inputs_identity(spec, cards, arts)
    state = published_state()
    problem = superset_problem(state, card_keys(cards))
    missing_runtime = [name for name, record in runtime_records(spec).items() if not object_present(record)]
    published_inputs = state["bundle"].get("inputsSha256") if state else None
    build = os.environ.get("FORCE") == "true" or published_inputs != inputs
    new = sort_keys(card_keys(cards) - card_keys(state["gallery"]["cards"])) if state else []
    counts = {kind: sum(c["kind"] == kind for c in cards) for kind in KINDS}
    summary("### Recognition bundle\n\n"
            + "".join(f"- {s['region']}: master {s['masterVersion']}\n" for s in sources)
            + f"- catalog: {counts['member']} Member, {counts['snap']} Snap cards"
            + (f"; not in the published gallery: {', '.join(new)}" if new else "") + "\n"
            + (f"- published bundle {state['pointer']['sha256'][:12]}\n" if state else "- nothing published yet\n")
            + "".join(f"- warning: {w}\n" for w in warnings)
            + (f"- pinned files missing from the bucket: {', '.join(missing_runtime)}\n" if missing_runtime else "")
            + f"- build: {'yes' if build else 'no (same inputs as the published bundle)'}")
    if problem:
        raise Failure(f"{problem}; the pointer stays on the published bundle")
    output("build", "true" if build else "false")
    output("runtime", "true" if missing_runtime else "false")


# ---------------------------------------------------------------- runtime files
def gh_json_lines(args: list[str]) -> list[dict]:
    text = subprocess.check_output(["gh", "api", *args], stderr=subprocess.PIPE, text=True)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def gh_download(repository: str, asset_id: int, target: Path) -> None:
    with target.open("wb") as stream:
        subprocess.run(["gh", "api", f"repos/{repository}/releases/assets/{asset_id}",
                        "-H", "Accept: application/octet-stream"], stdout=stream, stderr=subprocess.PIPE, check=True)


def download_release_files(spec: dict, names: list[str], work: Path) -> dict[str, Path]:
    """Pinned files from this repository's release `release` (a draft needs a token with push access)."""
    repository = env("GITHUB_REPOSITORY")
    found = gh_json_lines(["--paginate", f"repos/{repository}/releases", "--jq",
                           f'.[] | select(.tag_name == "{spec["release"]}") | '
                           '{id, draft, assets: [.assets[] | {id, name, size, state}]}'])
    require(len(found) == 1, f"release {spec['release']}: {len(found)} matching releases")
    assets = {asset["name"]: asset for asset in found[0]["assets"]}
    work.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in names:
        record = spec["files"][name]
        filename = f"{record['sha256']}.{record['ext']}"
        asset = assets.get(filename)
        require(asset and asset.get("state") == "uploaded" and asset.get("size") == record["bytes"],
                f"release {spec['release']}: asset {filename} missing or of another size")
        target = work / filename
        gh_download(repository, asset["id"], target)
        raw = target.read_bytes()
        require(len(raw) == record["bytes"] and digest(raw) == record["sha256"], f"{filename}: release bytes differ from the pin")
        files[name] = target
    return files


def cmd_runtime(work: Path, report_path: Path, stage: Path, dry_run: bool) -> None:
    spec = runtime_spec()
    records = runtime_records(spec)
    report = {"format": "moenotes.recognition-runtime-report/2", "dryRun": dry_run, "site": site_base(), "objects": [],
              "staged": []}
    states, served = {}, {}
    for name, record in records.items():
        observed = verify_public(record, missing_ok=True, keep=served if name in builder_inputs(spec) else None)
        states[name] = "existing" if observed else "missing"
    missing = [name for name, state in states.items() if state == "missing"]
    files = download_release_files(spec, missing, work) if missing else {}
    s3 = None if dry_run or not missing else s3_client()
    for name, record in records.items():
        row = {"name": name, **record, "state": states[name]}
        if name in files and not dry_run:
            row["state"], row["observed"] = put_asset(s3, record, files[name].read_bytes())
        elif name in files:
            row["state"] = "missing (checked release file; not uploaded: dry run)"
        report["objects"].append(row)
        write_json(report_path, report)
    stage.mkdir(parents=True, exist_ok=True)
    for name in builder_inputs(spec):
        record = records[name]
        raw = served[record["path"]] if name not in files else files[name].read_bytes()
        require(len(raw) == record["bytes"] and digest(raw) == record["sha256"], f"{name}: staged bytes differ from the pin")
        (stage / record["path"].removeprefix("assets/")).write_bytes(raw)
        report["staged"].append({"name": name, "sha256": record["sha256"], "bytes": record["bytes"],
                                 "from": "release" if name in files else "bucket"})
    report["complete"] = True
    write_json(report_path, report)
    summary(f"- pinned files: {len(records) - len(missing)} in the bucket, {len(missing)} "
            + ("checked from the release (dry run)" if dry_run else "uploaded from the release")
            + f"; {len(report['staged'])} encoder models staged for the gallery builder")


# ---------------------------------------------------------------- reference embeddings
def fit_box(width: float, height: float, size) -> list[float]:
    """ImageOps.fit's crop box (centering 0.5, no bleed): the largest centred box with the aspect of `size`."""
    live, wanted = width / height, size[0] / size[1]
    if live == wanted:
        crop_width, crop_height = width, height
    elif live >= wanted:
        crop_width, crop_height = wanted * height, height
    else:
        crop_width, crop_height = width, width / wanted
    left, top = (width - crop_width) * 0.5, (height - crop_height) * 0.5
    return [left, top, left + crop_width, top + crop_height]


def art_window(spec: dict, kind: str) -> tuple[float, float]:
    """The artwork window of a list tile in its logical units: the tile less `inset` on every side."""
    width, height = spec["pipeline"]["tiles"][kind]
    inset = spec["pipeline"]["encoders"][kind]["inset"]
    return width - 2 * inset, height - 2 * inset


def decode_artwork(raw: bytes, art: dict):
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        require(art["width"] is None or image.size == (art["width"], art["height"]),
                f"{art['assetPath']}: decoded size differs from the listing")
        rgb = image.convert("RGB")
    art["width"], art["height"] = rgb.size
    return rgb


def reference_input(spec: dict, kind: str, image) -> tuple:
    """The encoder input of a card's artwork (float32 3 x H x W, RGB in 0..1) and how it was made. A Member square is
    fitted (centred, Lanczos) to the artwork window, then resized bilinearly; a Snap image is cropped centred to the
    window's aspect and resized bilinearly in one step."""
    import numpy as np
    from PIL import Image
    height, width = spec["pipeline"]["encoders"][kind]["input"]
    window = tuple(round(v) for v in art_window(spec, kind))
    box = fit_box(image.width, image.height, window)
    if kind == "member":
        fitted = image.resize(window, Image.Resampling.LANCZOS, box=tuple(box))
        result = fitted.resize((width, height), Image.Resampling.BILINEAR)
        steps = [{"box": box, "size": list(window), "filter": "lanczos"}, {"size": [width, height], "filter": "bilinear"}]
    else:
        result = image.resize((width, height), Image.Resampling.BILINEAR, box=tuple(box))
        steps = [{"box": box, "size": [width, height], "filter": "bilinear"}]
    array = np.asarray(result, np.float32).transpose(2, 0, 1) / 255.
    return np.ascontiguousarray(array), steps


def run_encoder(model: Path, batch):
    """L2-normalized embeddings of an [N, 3, H, W] batch with ONNX Runtime on one CPU thread."""
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
    inputs, outputs = session.get_inputs(), session.get_outputs()
    require([i.name for i in inputs] == ["image"] and [o.name for o in outputs] == ["embedding"],
            f"{model.name}: the encoder takes `image` and returns `embedding`")
    return session.run(["embedding"], {"image": batch})[0]


def build_encoder_gallery(spec: dict, cards: list[dict], images: list, models: dict[str, Path]) -> dict:
    """Per kind: the reference embedding of every card (float32 rows, the order of `cards` of that kind)."""
    import numpy as np
    out = {}
    for kind in KINDS:
        indices = [index for index, card in enumerate(cards) if card["kind"] == kind]
        inputs, steps = zip(*(reference_input(spec, kind, images[index]) for index in indices))
        name = spec["pipeline"]["encoders"][kind]["model"]
        vectors = np.vstack([run_encoder(models[name], np.stack(inputs[start:start + 32]))
                             for start in range(0, len(indices), 32)]).astype("<f4")
        require(vectors.ndim == 2 and vectors.shape[0] == len(indices) and vectors.shape[1] >= 1, f"{kind} embeddings: shape")
        require(np.isfinite(vectors).all() and np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-3),
                f"{kind} embeddings are not unit vectors")
        out[kind] = {"cards": indices, "vectors": vectors, "steps": list(steps), "model": name}
    return out


# Gallery builders by recognition-runtime.json `galleries.<name>.builder`.
BUILDERS = {"encoder-embed/1": build_encoder_gallery}


def file_reference(record: dict) -> dict:
    return {"file": record["path"].removeprefix("assets/"), "sha256": record["sha256"], "bytes": record["bytes"]}


def models_manifest(spec: dict, pins: dict) -> dict:
    """The Worker's runtime and model stages: every model by file reference, with its parameters from the pins."""
    pipeline = spec["pipeline"]

    def stage(item: dict) -> dict:
        return {**item, "model": file_reference(pins[item["model"]])}

    return {"format": MODELS_FORMAT, "source": spec["source"],
            "runtime": {role: file_reference(pins[name]) for role, name in spec["runtime"].items()},
            "tiles": pipeline["tiles"], "locator": stage(pipeline["locator"]),
            **{name: {kind: stage(item) for kind, item in pipeline[name].items()} for name in STAGES}}


# ---------------------------------------------------------------- build
def cmd_build(out: Path, models_dir: Path, runtime_dir: Path | None) -> None:
    spec = runtime_spec()
    check_requirements(spec)
    require(not out.exists() or not any(out.iterdir()), f"{out} is not empty")
    models = {name: pinned_file(models_dir, name, spec["files"][name]) for name in builder_inputs(spec)}
    site = out / "site"
    (site / "assets").mkdir(parents=True)
    sources = exported_sources(regions())
    cards, warnings = catalog(sources)
    arts = resolve_art(cards)
    state = published_state()
    problem = superset_problem(state, card_keys(cards))
    require(not problem, f"{problem}; nothing was built")
    api = env("RECOGNITION_ASSET_API", "https://assets.bdon.moe").rstrip("/")
    files = {}

    def add(name: str, raw: bytes, ext: str) -> dict:
        record = {"path": f"assets/{digest(raw)}.{ext}", "sha256": digest(raw), "bytes": len(raw), "contentType": TYPES[ext]}
        files[name] = record
        (site / record["path"]).write_bytes(raw)
        return record

    def download(art: dict) -> bytes:
        raw = fetch(api + art["file"], timeout=180).body
        require(len(raw) == art["bytes"] and digest(raw) == art["sha256"],
                f"{art['assetPath']}: downloaded bytes differ from the listing")
        return raw

    with concurrent.futures.ThreadPoolExecutor(6) as executor:
        artwork = list(executor.map(download, arts))
    images = [decode_artwork(raw, art) for raw, art in zip(artwork, arts)]
    builder = spec["galleries"]["gallery"]["builder"]
    embeddings = BUILDERS[builder](spec, cards, images, models)
    pins = runtime_records(spec)
    records = [{"kind": c["kind"], "id": c["id"], "identity": c["identity"], "regions": c["regions"], "levelLimit": c["levelLimit"],
                "art": {"assetPath": a["assetPath"], "bytes": a["bytes"], "sha256": a["sha256"], "width": a["width"],
                        "height": a["height"]}} for c, a in zip(cards, arts)]
    sections = {}
    for kind, item in embeddings.items():
        for index, steps in zip(item["cards"], item["steps"]):
            records[index]["art"]["reference"] = steps
        record = add(f"gallery/{kind}.f32.bin", item["vectors"].tobytes(), "bin")
        sections[kind] = {"encoder": file_reference(pins[item["model"]]), "dimension": int(item["vectors"].shape[1]),
                          "cards": item["cards"], "buffer": {**file_reference(record), "shape": list(item["vectors"].shape)}}
    counts = {kind: sum(c["kind"] == kind for c in cards) for kind in KINDS}
    gallery = js_numbers({
        "format": GALLERY_FORMAT, "source": spec["source"], "builder": builder,
        "catalog": [{"region": s["region"], "masterVersion": s["masterVersion"], "tables": s["tables"]} for s in sources],
        "cards": records, "embeddings": sections, "scope": SCOPE.format(**counts),
    })
    gallery["galleryId"] = digest(canonical(gallery))
    closure = {"gallery/manifest.json": add("gallery/manifest.json", document(gallery), "json")}
    closure.update({f"gallery/{kind}.f32.bin": files[f"gallery/{kind}.f32.bin"] for kind in embeddings})
    closure["models/manifest.json"] = add("models/manifest.json", document(models_manifest(spec, pins)), "json")
    closure.update(pins)
    inputs = inputs_identity(spec, cards, arts)
    bundle = {"format": BUNDLE_FORMAT, "inputsSha256": inputs,
              "previous": state["pointer"] if state else None,
              "entries": {"gallery": "gallery/manifest.json", "models": "models/manifest.json"},
              "counts": counts, "files": closure}
    bundle_record = add("bundle.json", document(bundle), "json")
    if runtime_dir:
        for name, record in pins.items():
            shutil.copyfile(pinned_file(runtime_dir, name, spec["files"][name]), site / record["path"])
    pointer = pointer_document(bundle_record["sha256"], bundle_record["bytes"])
    (site / POINTER).parent.mkdir(parents=True, exist_ok=True)
    (site / POINTER).write_bytes(pointer)
    previous_cards = {f"{c['kind']}:{c['id']}": c for c in state["gallery"]["cards"]} if state else {}
    report = {"format": "moenotes.recognition-build/2", "inputsSha256": inputs, "previous": bundle["previous"],
              "bundle": {"sha256": bundle_record["sha256"], "bytes": bundle_record["bytes"]},
              "gallery": {**{k: closure["gallery/manifest.json"][k] for k in ("sha256", "bytes")}, "galleryId": gallery["galleryId"],
                          "rows": {kind: len(item["cards"]) for kind, item in embeddings.items()}},
              "models": {k: closure["models/manifest.json"][k] for k in ("sha256", "bytes")},
              "counts": counts, "catalog": gallery["catalog"],
              "newCards": sort_keys(card_keys(cards) - set(previous_cards)),
              "changedArtwork": [f"{r['kind']}:{r['id']}" for r in records
                                 if f"{r['kind']}:{r['id']}" in previous_cards
                                 and previous_cards[f"{r['kind']}:{r['id']}"].get("art", {}).get("sha256") != r["art"]["sha256"]],
              "warnings": warnings, "runtimeIncluded": bool(runtime_dir),
              "objects": [{"name": name, **record, "origin": "runtime" if name in pins else "build"} for name, record in closure.items()]}
    write_json(out / "report.json", report)
    summary(f"- built bundle {bundle_record['sha256'][:12]}: {counts['member']} Member, {counts['snap']} Snap cards, "
            f"embeddings by {builder}"
            + ("; the first bundle" if state is None else f"; new: {', '.join(report['newCards'])}" if report["newCards"] else "")
            + (f"; changed artwork: {', '.join(report['changedArtwork'])}" if report["changedArtwork"] else ""))


# ---------------------------------------------------------------- publish
def s3_client():
    key, secret = os.environ.get("STORY_S3_ACCESS_KEY"), os.environ.get("STORY_S3_SECRET_KEY")
    require(bool(key and secret), "publishing needs STORY_S3_ACCESS_KEY and STORY_S3_SECRET_KEY")
    import boto3
    from botocore.config import Config
    # S3-compatible stores (SeaweedFS here) take path-style requests and no default CRC checksums
    config = Config(s3={"addressing_style": "path"}, request_checksum_calculation="when_required",
                    response_checksum_validation="when_required", retries={"max_attempts": 5, "mode": "adaptive"},
                    max_pool_connections=8, connect_timeout=15, read_timeout=120)
    return boto3.client("s3", endpoint_url=env("STORY_S3_ENDPOINT", "https://storage.bdon.moe"), aws_access_key_id=key,
                        aws_secret_access_key=secret, region_name="us-east-1", config=config)


def read_back(record: dict) -> dict:
    for attempt in range(5):
        observed = verify_public(record, missing_ok=True)
        if observed:
            return observed
        time.sleep(2 + 3 * attempt)
    raise Failure(f"{record['path']}: not served after upload")


def put_asset(s3, record: dict, body: bytes) -> tuple[str, dict]:
    """Create-only upload; an object that appeared meanwhile is checked like any existing one."""
    require(len(body) == record["bytes"] and digest(body) == record["sha256"], f"{record['path']}: local bytes changed")
    try:
        s3.put_object(Bucket=env("STORY_S3_BUCKET", "moenotes"), Key=object_key(record["path"]), Body=body,
                      ContentType=record["contentType"], CacheControl=ASSET_CACHE, IfNoneMatch="*")
        state = "uploaded"
    except Exception as error:
        code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
        if code not in ("PreconditionFailed", "ConditionalRequestConflict", "412"):
            raise
        state = "existing (created concurrently)"
    return state, read_back(record)


def local_closure(out: Path, spec: dict) -> tuple[dict, bytes, dict]:
    """The built bundle: pointer, bundle manifest and every object, checked against each other."""
    site = out / "site"
    pointer_raw = (site / POINTER).read_bytes()
    pointer = parse_pointer(pointer_raw)
    bundle_raw = (site / f"assets/{pointer['sha256']}.json").read_bytes()
    require(len(bundle_raw) == pointer["bytes"] and digest(bundle_raw) == pointer["sha256"], "bundle manifest differs from the pointer")
    bundle = json.loads(bundle_raw)
    require(bundle.get("format") == BUNDLE_FORMAT, "bundle manifest format")
    pins = runtime_records(spec)
    for name, record in bundle["files"].items():
        match = ASSET_PATH.fullmatch(record.get("path", ""))
        require(match and match.group(1) == record["sha256"] and match.group(2) in TYPES
                and record["contentType"] == TYPES[match.group(2)], f"{name}: invalid object record")
        if name in pins:
            require(pins[name] == record, f"{name}: differs from recognition-runtime.json")
        path = site / record["path"]
        if path.is_file():
            raw = path.read_bytes()
            require(len(raw) == record["bytes"] and digest(raw) == record["sha256"], f"{name}: local bytes differ")
        else:
            require(name in pins, f"{name}: built object missing from {site}")
    entries = bundle["entries"]
    require(set(entries) == {"gallery", "models"}, "the bundle names its gallery and models manifests")
    gallery = json.loads((site / bundle["files"][entries["gallery"]]["path"]).read_bytes())
    claimed = gallery.pop("galleryId")
    require(digest(canonical(gallery)) == claimed, "galleryId differs from the gallery manifest")
    models = json.loads((site / bundle["files"][entries["models"]]["path"]).read_bytes())
    require(models == js_numbers(models_manifest(spec, pins)), "the models manifest differs from recognition-runtime.json")
    referenced = {name: pins[name]["sha256"] for name in pins}
    for kind, section in gallery["embeddings"].items():
        referenced[f"gallery/{kind}.f32.bin"] = section["buffer"]["sha256"]
        require(section["encoder"]["sha256"] == models["encoders"][kind]["model"]["sha256"],
                f"{kind} embeddings were made by another encoder than the bundle's")
    for name, sha in referenced.items():
        require(bundle["files"].get(name, {}).get("sha256") == sha, f"{name}: manifests and bundle differ")
    require(set(referenced) | set(entries.values()) == set(bundle["files"]), "the bundle lists objects no manifest uses")
    return bundle, pointer_raw, pointer


def cmd_publish(out: Path, dry_run: bool) -> None:
    spec = runtime_spec()
    bundle, pointer_raw, pointer = local_closure(out, spec)
    site, pins = out / "site", runtime_records(spec)
    report_path = out / "publication.json"
    gallery = json.loads((site / bundle["files"][bundle["entries"]["gallery"]]["path"]).read_bytes())
    report = {"format": "moenotes.recognition-publication/1", "dryRun": dry_run, "site": site_base(),
              "bundle": pointer, "previous": bundle["previous"], "objects": [], "pointerWritten": False, "complete": False}
    names = list(bundle["files"])
    with concurrent.futures.ThreadPoolExecutor(4) as executor:
        observed = dict(zip(names, executor.map(lambda n: verify_public(bundle["files"][n], missing_ok=True), names)))
    bundle_name = "bundle.json"
    bundle_record = {"path": f"assets/{pointer['sha256']}.json", "sha256": pointer["sha256"], "bytes": pointer["bytes"],
                     "contentType": TYPES["json"]}
    observed[bundle_name] = verify_public(bundle_record, missing_ok=True)
    records = {**bundle["files"], bundle_name: bundle_record}
    rows = {name: {"name": name, **records[name], "state": "existing" if observed[name] else "missing",
                   **({"observed": observed[name]} if observed[name] else {})} for name in records}
    report["objects"] = list(rows.values())
    missing = [name for name in records if not observed[name]]
    unavailable = [name for name in missing if name in pins]
    state = published_state()
    current = state["pointer"] if state else None
    report["pointerBefore"] = current
    report["counts"] = {"objects": len(records), "existing": len(records) - len(missing), "missing": len(missing),
                        "missingBytes": sum(records[n]["bytes"] for n in missing)}
    write_json(report_path, report)
    problem = superset_problem(state, card_keys(gallery["cards"]))
    require(not problem, f"{problem}; the pointer stays on the published bundle")
    # The pointer may only advance from the bundle this one was built on (or already name it: a re-run).
    require(current in (bundle["previous"], pointer), "the published pointer changed since this bundle was built; build again")
    if dry_run:
        report["unavailableRuntime"] = unavailable
        write_json(report_path, report)
        own = [n for n in missing if n not in unavailable]
        summary(f"- dry run: {report['counts']['existing']} objects in the bucket, {len(own)} to upload "
                f"({sum(records[n]['bytes'] for n in own) / 1e6:.1f} MB)"
                + (f", pinned files the runtime job uploads first: {', '.join(unavailable)}" if unavailable else "")
                + f"; the pointer would name {pointer['sha256'][:12]}")
        return
    require(not unavailable, f"pinned file(s) missing from the bucket: {', '.join(unavailable)} (the runtime job uploads them)")
    s3 = s3_client() if missing or current != pointer else None
    manifests = set(bundle["entries"].values())
    groups = [[n for n in missing if n not in manifests and n != bundle_name],
              [n for n in missing if n in manifests], [n for n in missing if n == bundle_name]]
    for group in groups:
        def upload(name):
            return name, put_asset(s3, records[name], (site / records[name]["path"]).read_bytes())
        with concurrent.futures.ThreadPoolExecutor(4) as executor:
            for name, (state_name, seen) in executor.map(upload, group):
                rows[name].update(state=state_name, observed=seen)
                print(json.dumps({"uploaded": records[name]["path"], "bytes": records[name]["bytes"]}), flush=True)
                write_json(report_path, report)
    # Every object of the bundle, as browsers will get it, before the pointer names it.
    with concurrent.futures.ThreadPoolExecutor(4) as executor:
        for name, seen in zip(records, executor.map(lambda n: verify_public(records[n]), list(records))):
            rows[name]["verified"] = seen
    write_json(report_path, report)
    latest = published_state()
    require((latest["pointer"] if latest else None) in (bundle["previous"], pointer),
            "the published pointer changed during publication; the new objects stay, the pointer is not moved")
    require(not superset_problem(latest, card_keys(gallery["cards"])), "the published gallery changed during publication")
    if current != pointer:
        s3.put_object(Bucket=env("STORY_S3_BUCKET", "moenotes"), Key=object_key(POINTER), Body=pointer_raw,
                      ContentType=TYPES["json"], CacheControl=POINTER_CACHE)
        report["pointerWritten"] = True
    for attempt in range(6):
        response = fetch(site_base() + POINTER, origin=True, missing_ok=True)
        if response and response.body == pointer_raw:
            break
        time.sleep(5 * (attempt + 1))
    else:
        write_json(report_path, report)
        raise Failure("the pointer was written but is not served yet; check recognition/current.json")
    report["complete"] = True
    write_json(report_path, report)
    uploaded = sum(row["state"] == "uploaded" for row in rows.values())
    summary(f"- published bundle {pointer['sha256'][:12]}: {uploaded} objects uploaded, "
            f"{len(records) - uploaded} already in the bucket; pointer {'moved' if report['pointerWritten'] else 'unchanged'}")


# ---------------------------------------------------------------- main
def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    commands.add_parser("requirements")
    runtime = commands.add_parser("runtime")
    runtime.add_argument("--work", type=Path, required=True)
    runtime.add_argument("--report", type=Path, required=True)
    runtime.add_argument("--stage", type=Path, required=True)
    runtime.add_argument("--dry-run", action="store_true")
    build = commands.add_parser("build")
    build.add_argument("out", type=Path)
    build.add_argument("--models", type=Path, required=True)
    build.add_argument("--runtime-dir", type=Path)
    publish = commands.add_parser("publish")
    publish.add_argument("out", type=Path)
    publish.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            cmd_plan()
        elif args.command == "requirements":
            print(" ".join(requirements(runtime_spec())))
        elif args.command == "runtime":
            cmd_runtime(args.work, args.report, args.stage, args.dry_run)
        elif args.command == "build":
            cmd_build(args.out, args.models, args.runtime_dir)
        else:
            cmd_publish(args.out, args.dry_run)
    except Failure as error:
        failure = {"complete": False, "reason": str(error)}
        if args.command in ("build", "publish"):
            write_json(args.out / "failure.json", failure)
        summary(f"- stopped: {error}")
        sys.exit(f"recognition: {error}")


if __name__ == "__main__":
    main(sys.argv[1:])
