#!/usr/bin/env python3
"""The story site's CI steps (.github/workflows/story-site.yml): the published site lives in an S3 bucket, a run adds
the stories it lacks.

    regions               the regions this run builds (GitHub outputs `regions`, a JSON list, and `count`): those of
                          $STORY_REGIONS (default hk-tw-mo) that the repository_dispatch payload names, or
                          $REQUESTED_REGION of a workflow_dispatch (empty / "all": every one), or all on a schedule
    plan                  the stories to build: MasterAdv ids absent from this site's index and, for JP, the international
                          site ($STORY_S3_PREFIX_SHARED); $REQUESTED narrows the ids, $FORCE rebuilds requested ids
                          (at most $STORY_LIMIT, in id order); GitHub outputs `stories` (space separated) and `count`
    master OUT            the decoded master data of $MASTERDATA_REGION from moenotes-masterdata-sync (index.json:
                          every table with its SHA-256), for `nnnotes web`
    fetch SITE            the public indexes and the manifests they reference into SITE, with their SHA-256 in
                          SITE.fetched.json; an unpublished site starts empty
    build SITE ID...      `nnnotes web SITE --story ID ...` ($FORCE: --force), then `--player-only`, which rewrites the
                          player files and the indexes from every manifest present even after a failed build
    publish SITE [--dry-run]
                          the new assets (checked by HEAD), then every other file that is new or changed since
                          `fetch`, the indexes (stories.json, models.json, charts.json) last; never deletes

Bucket: $STORY_S3_ENDPOINT, $STORY_S3_BUCKET, $STORY_S3_PREFIX (a key prefix, may be empty), credentials
$STORY_S3_ACCESS_KEY / $STORY_S3_SECRET_KEY (unset: anonymous, reads only). Public indexes list the manifests to fetch;
S3 listing is not used because the gateway caches different query parameters as one response. Objects are stored as nnnotes writes them:
a compressed asset (.gz / .br) as it is, without Content-Encoding (the player decodes it).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

INDEXES = ("stories.json", "models.json", "charts.json")
ASSETS = "assets/"
ASSET_CACHE = "public, max-age=31536000, immutable"       # content-addressed: never changes
OTHER_CACHE = "public, max-age=300"
TYPES = {".json": "application/json", ".gz": "application/gzip", ".br": "application/octet-stream",
         ".m4a": "audio/mp4", ".mp4": "video/mp4", ".webm": "video/webm", ".png": "image/png", ".jpg": "image/jpeg",
         ".glsl": "text/plain; charset=utf-8", ".html": "text/html; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".map": "application/json", ".txt": "text/plain; charset=utf-8",
         ".svg": "image/svg+xml", ".wasm": "application/wasm", ".bin": "application/octet-stream"}


def get(url: str, timeout: int = 120) -> bytes:
    # an explicit User-Agent: Cloudflare in front of the services refuses Python-urllib's
    request = urllib.request.Request(url, headers={"User-Agent": "moenotes-story-site (GitHub Actions)",
                                                   "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=timeout) as r:
        return r.read()


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, "")
    if value:
        return value
    if default is None:
        sys.exit(f"story_site: set {name}")
    return default


def output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    print(f"{name}={value}")


def summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)


# ---------------------------------------------------------------- bucket
class Bucket:
    def __init__(self, prefix: str | None = None):
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config
        key, secret = os.environ.get("STORY_S3_ACCESS_KEY"), os.environ.get("STORY_S3_SECRET_KEY")
        # S3-compatible stores (SeaweedFS here) take path-style requests and no default CRC checksums
        config = Config(s3={"addressing_style": "path"}, retries={"max_attempts": 8, "mode": "adaptive"},
                        request_checksum_calculation="when_required", response_checksum_validation="when_required",
                        max_pool_connections=32, **({} if key and secret else {"signature_version": UNSIGNED}))
        self.writable = bool(key and secret)
        self.s3 = boto3.client("s3", endpoint_url=env("STORY_S3_ENDPOINT"), aws_access_key_id=key or None,
                               aws_secret_access_key=secret or None, region_name="us-east-1", config=config)
        self.name = env("STORY_S3_BUCKET")
        prefix = (os.environ.get("STORY_S3_PREFIX", "") if prefix is None else prefix).strip("/")
        self.prefix = f"{prefix}/" if prefix else ""

    def read(self, path: str, *, missing_ok: bool = False) -> bytes | None:
        """Read one public object by its path; indexes may be absent on a new site."""
        url = f"{env('STORY_S3_ENDPOINT').rstrip('/')}/{self.name}/{self.prefix}{path}"
        try:
            return get(url, 300)
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise RuntimeError(f"story_site: cannot fetch {url}: HTTP {error.code}") from error

    def published_story_ids(self) -> set[int]:
        """Stories advertised to the player, over public HTTP (anonymous S3 listings can be empty)."""
        data = self.read("stories.json", missing_ok=True)
        if data is None:
            return set()
        index = json.loads(data)
        return {entry["advId"] for entry in index["stories"]}

    def object_size(self, path: str) -> int | None:
        """HEAD has an object-specific URL; bucket listings are cached without their query parameters."""
        from botocore.exceptions import ClientError
        try:
            return self.s3.head_object(Bucket=self.name, Key=self.prefix + path)["ContentLength"]
        except ClientError as error:
            if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def upload(self, path: str, src: Path) -> None:
        suffix = Path(path).suffix.lower()
        extra = {"ContentType": TYPES.get(suffix, "application/octet-stream"),
                 "CacheControl": ASSET_CACHE if path.startswith(ASSETS) else OTHER_CACHE}
        self.s3.upload_file(str(src), self.name, self.prefix + path, ExtraArgs=extra)


def parallel(fn, items, workers: int = 16) -> None:
    with ThreadPoolExecutor(workers) as ex:
        for _ in ex.map(fn, items):
            pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- master data
def master_index() -> tuple[str, dict]:
    base = env("MASTERDATA_BASE_URL").rstrip("/")
    index = json.loads(get(f"{base}/index.json", 60))
    region = index["regions"][env("MASTERDATA_REGION")]
    if not re.fullmatch(r"/([a-z]+/)?master/", region["path"]):
        sys.exit(f"story_site: unexpected master path {region['path']!r}")
    return base + region["path"], region


# Regions with a story site (story-site-region.yml gives each its own bucket prefix: JP model ids overlap the
# international ones).
SITE_REGIONS = ("hk-tw-mo", "jp")


def split_list(text: str) -> list[str]:
    return [part for part in re.split(r"[\s,]+", text.strip()) if part]


def select_regions(enabled: str, event: str, payload: list[str] | None, requested: str) -> list[str]:
    """The regions a run builds: of $STORY_REGIONS, the dispatch's regions, the requested one, or all (schedule)."""
    allowed = [r for r in split_list(enabled) if r in SITE_REGIONS]
    unknown = [r for r in split_list(enabled) if r not in SITE_REGIONS]
    if unknown:
        sys.exit(f"story_site: no story site for region(s) {', '.join(unknown)}")
    if event == "repository_dispatch":
        # Older dispatches carry no regions: build every enabled one (a run without new stories ends at plan).
        wanted = payload if payload else allowed
    elif event == "workflow_dispatch" and requested and requested != "all":
        wanted = [requested]
    else:
        wanted = allowed
    return [r for r in allowed if r in wanted]


def cmd_regions() -> None:
    payload = None
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and Path(event_path).is_file():
        regions = (json.loads(Path(event_path).read_text(encoding="utf-8")).get("client_payload") or {}).get("regions")
        payload = [r for r in regions if isinstance(r, str)] if isinstance(regions, list) else None
    regions = select_regions(env("STORY_REGIONS", "hk-tw-mo"), env("GITHUB_EVENT_NAME", ""), payload,
                             os.environ.get("REQUESTED_REGION", ""))
    output("regions", json.dumps(regions))
    output("count", str(len(regions)))


def configure_region() -> None:
    """Keep build inputs from one release; JP client/API/CDN are read from the public snapshot."""
    region = env("MASTERDATA_REGION")
    names = {"hk-tw-mo": "tw", "en": "en", "kr": "kr", "jp": "jp"}
    if region not in names or env("NNNOTES_CATALOG_REGION") != names[region]:
        sys.exit("story_site: master region and catalog region differ")
    if region != "jp":
        return
    _, snapshot = master_index()
    entry = snapshot.get("entry") or {}
    assets = entry.get("assets") or {}
    upstream = entry.get("upstream") or {}
    api = assets.get("api_root") or upstream.get("api_root")
    bundle = assets.get("bundle_root")
    from urllib.parse import urlsplit
    cdn = upstream.get("cdn_root")
    if bundle:
        u = urlsplit(bundle)
        cdn = f"{u.scheme}://{u.netloc}"
    client = entry.get("client_version")
    if not all(isinstance(v, str) and v for v in (api, cdn, client)):
        sys.exit("story_site: JP snapshot lacks API/CDN/client metadata")
    os.environ.update(NNNOTES_SERVERS_JP_API=api, NNNOTES_SERVERS_JP_CDN=cdn,
                      NNNOTES_SERVERS_JP_CLIENT_VERSION=client, NNNOTES_SERVERS_JP_PROVIDER="jp")


def fetch_table(url: str, name: str, digest: str, dest: Path) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.json", name):
        sys.exit(f"story_site: unexpected master file name {name!r}")
    for _ in range(4):
        data = get(url + name)
        if hashlib.sha256(data).hexdigest() == digest:
            dest.write_bytes(data)
            return
    sys.exit(f"story_site: {name}: SHA-256 differs from index.json (the service changed snapshots? run again)")


def cmd_master(out: str) -> None:
    url, region = master_index()
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    parallel(lambda item: fetch_table(url, item[0], item[1], out_dir / item[0]), region["files"].items(), 8)
    print(f"master data: {len(region['files'])} tables into {out_dir}")


# ---------------------------------------------------------------- plan
def parse_ids(text: str) -> list[int]:
    ids = []
    for token in re.split(r"[\s,]+", text.strip()):
        if token:
            if not token.isdigit():
                sys.exit(f"story_site: not a story id: {token!r}")
            ids.append(int(token))
    return list(dict.fromkeys(ids))


def cmd_plan() -> None:
    url, region = master_index()
    data = get(url + "MasterAdv.json")
    if hashlib.sha256(data).hexdigest() != region["files"]["MasterAdv.json"]:
        sys.exit("story_site: MasterAdv.json: SHA-256 differs from index.json (run again)")
    stories = sorted(row["_id"] for row in json.loads(data)["_allData"])   # nnnotes storysite.all_stories
    have = Bucket().published_story_ids()
    shared = set()
    if env("MASTERDATA_REGION") == "jp":
        shared = Bucket(prefix=os.environ.get("STORY_S3_PREFIX_SHARED", "")).published_story_ids()
    available = have | shared
    reused = set(stories) & (shared - have)
    requested = parse_ids(os.environ.get("REQUESTED", ""))
    if requested:
        unknown = [i for i in requested if i not in set(stories)]
        if unknown:
            sys.exit(f"story_site: no MasterAdv row for {', '.join(map(str, unknown))}")
        missing = [i for i in requested if i not in available]
        todo = requested if os.environ.get("FORCE") == "true" else missing
    else:
        missing = [i for i in stories if i not in available]
        todo = missing[:int(env("STORY_LIMIT", "40"))]
    summary(f"### Story site\n\n- master: {region.get('entry', {}).get('version', '?')} "
            f"({len(stories)} stories), the site has {len(have)}\n"
            + (f"- reused from the international site: {len(reused)}\n" if env("MASTERDATA_REGION") == "jp" else "")
            + f"- missing: {len(missing)}; this run builds "
            f"{len(todo)}" + (f": {' '.join(map(str, todo))}" if todo else ""))
    output("stories", " ".join(map(str, todo)))
    output("count", str(len(todo)))


# ---------------------------------------------------------------- fetch / build / publish
def fetched_file(site: Path) -> Path:
    return site.parent / f"{site.name}.fetched.json"


def cmd_fetch(site_dir: str) -> None:
    site, bucket = Path(site_dir), Bucket()
    site.mkdir(parents=True, exist_ok=True)
    indexes, manifests = [], set()
    # Follow the same indexes as the player. This also keeps JP out of the international fetch; cached S3 lists
    # at the bucket URL ignore Prefix/continuation-token and can return another region's objects.
    for name in INDEXES:
        data = bucket.read(name, missing_ok=True)
        if data is None:
            continue
        index = json.loads(data)
        for entry in index[name.removesuffix(".json")]:
            path = entry["manifest"]
            parts = path.split("/")
            if (parts[0] != name.removesuffix(".json") or not path.endswith(".json")
                    or any(p in ("", ".", "..") or "\\" in p or ":" in p for p in parts)):
                sys.exit(f"story_site: unexpected manifest path {path!r} in {name}")
            manifests.add(path)
        (site / name).write_bytes(data)
        indexes.append(name)
    def fetch(key: str) -> None:
        dest = site / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(bucket.read(key))

    parallel(fetch, sorted(manifests))
    (site / "assets").mkdir(parents=True, exist_ok=True)
    keys = indexes + sorted(manifests)
    digests = {k: sha256(site / k) for k in keys}
    fetched_file(site).write_text(json.dumps(digests, indent=1, sort_keys=True), encoding="utf-8")
    print(f"fetched {len(keys)} files ({sum((site / k).stat().st_size for k in keys) / 1e6:.1f} MB) into {site}")


def check_jp_api() -> None:
    print("Checking JP Version access before starting build workers", flush=True)
    check = subprocess.run([sys.executable, "-m", "nnnotes", "master", "version"],
                           stdout=subprocess.PIPE, timeout=60)
    if check.returncode:
        sys.exit("story_site: JP Version preflight failed before starting workers; "
                 "the runner must be able to access the JP API to obtain CDN authentication")


def partition_story_assets(stories: list[int], rows: list[dict], has) -> tuple[list[int], list[dict]]:
    """Defer only episodes whose main script is absent; other resource/export errors remain build failures."""
    by_id = {row["_id"]: row for row in rows}
    ready, deferred = [], []
    for story in stories:
        asset = by_id[story]["_advEpisodeAsset"]
        key = f"Adv/Episode/{asset}/{asset}"
        if has(key):
            ready.append(story)
        else:
            deferred.append({"id": story, "asset": key, "reason": "episode script absent from catalog"})
    return ready, deferred


def check_jp_story_assets(stories: list[int]) -> tuple[list[int], list[dict]]:
    from nnnotes.cli import master_dir, open_catalog
    from nnnotes.config import Config

    cfg = Config.load()
    # Use the same remote + APK catalog as the exporter; embedded episodes must remain buildable.
    cat = open_catalog(cfg, bundles=False)
    rows = json.loads((master_dir(cfg) / "MasterAdv.json").read_bytes())["_allData"]
    return partition_story_assets(stories, rows, cat.has)


def cmd_build(site_dir: str, ids: list[str]) -> None:
    configure_region()
    site = Path(site_dir).resolve()
    stories = parse_ids(" ".join(ids))
    tmp = site.parent / f"{site.name}.tmp"
    nnnotes = [sys.executable, "-m", "nnnotes", "web", str(site), "--tmp", str(tmp)]
    if env("MASTERDATA_REGION") == "jp":
        nnnotes += ["--story-languages", "ja"]
        if stories:
            # A Pool keeps replacing workers whose initializer cannot open the JP catalog. Check the API once
            # in the parent before any pool starts so a denied runner fails promptly with the original error.
            check_jp_api()
            stories, deferred = check_jp_story_assets(stories)
            (site.parent / f"{site.name}.deferred.json").write_text(json.dumps(deferred, indent=1), encoding="utf-8")
            if deferred:
                summary(f"- deferred {len(deferred)} stories: episode script absent from the current catalog; "
                        "retry on the next run\n" + "\n".join(f"  - {r['id']}: `{r['asset']}`" for r in deferred))
    status = 0
    report = site.parent / f"{site.name}.build.json"
    report.unlink(missing_ok=True)
    if stories:
        cmd = nnnotes + [a for i in stories for a in ("--story", str(i))]
        cmd += ["--workers", env("STORY_WORKERS", "2")]
        if os.environ.get("FORCE") == "true":
            cmd.append("--force")
        print("+ " + " ".join(cmd[1:]), flush=True)
        with report.open("wb") as report_stream:
            status = subprocess.run(cmd, stdout=report_stream).returncode
    # The indexes and player files from every manifest present, whatever the build did.
    index = subprocess.run(nnnotes + ["--player-only"], stdout=subprocess.PIPE).returncode
    if report.is_file():
        try:
            r = json.loads(report.read_text(encoding="utf-8"))
            summary(f"- built {len(r.get('storiesBuilt', []))}, failed {len(r.get('storiesFailed', []))}, "
                    f"skipped {len(r.get('storiesSkipped', []))}; models built "
                    f"{len((r.get('storyModels') or {}).get('modelsBuilt') or [])} in {r.get('storySeconds', '?')} s")
        except ValueError:
            pass
    if status or index:
        sys.exit(f"story_site: nnnotes web exited with {status or index}")


def cmd_publish(site_dir: str, dry_run: bool = False) -> None:
    site, bucket = Path(site_dir), Bucket()
    if not bucket.writable and not dry_run:
        sys.exit("story_site: publish needs STORY_S3_ACCESS_KEY and STORY_S3_SECRET_KEY")
    before = json.loads(fetched_file(site).read_text(encoding="utf-8"))
    local = sorted(p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file())
    assets = [k for k in local if k.startswith(ASSETS)]
    with ThreadPoolExecutor(max_workers=16) as executor:
        have = dict(zip(assets, executor.map(bucket.object_size, assets)))
    new_assets = [k for k in assets if have.get(k) != (site / k).stat().st_size]
    changed = [k for k in local if not k.startswith(ASSETS) and before.get(k) != sha256(site / k)]
    last = [k for k in changed if k in INDEXES]
    first = [k for k in changed if k not in INDEXES]
    if dry_run:
        for k in first + last:
            print(f"would upload {k}")
    else:
        # manifests before the indexes that list them, assets before the manifests that name them
        for group in (new_assets, first, last):
            parallel(lambda k: bucket.upload(k, site / k), group)
    summary(f"- {'would publish' if dry_run else 'published'} {len(new_assets)} assets ({sum((site / k).stat().st_size for k in new_assets) / 1e6:.1f} MB)"
            f", {len(first)} files, indexes: {', '.join(last) or 'unchanged'}")


def main(argv: list[str]) -> None:
    if not argv:
        sys.exit(__doc__)
    cmd, args = argv[0], argv[1:]
    if cmd == "regions" and not args:
        cmd_regions()
    elif cmd == "jp-check" and not args:
        configure_region()
        check_jp_api()
    elif cmd == "plan" and not args:
        cmd_plan()
    elif cmd == "master" and len(args) == 1:
        cmd_master(args[0])
    elif cmd == "fetch" and len(args) == 1:
        cmd_fetch(args[0])
    elif cmd == "build" and args:
        cmd_build(args[0], args[1:])
    elif cmd == "publish" and len(args) in (1, 2) and args[1:] in ([], ["--dry-run"]):
        cmd_publish(args[0], dry_run=bool(args[1:]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
