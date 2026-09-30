"""Story workflow: reuse international advIds and fetch/publish through object URLs instead of cached listings."""
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import story_site


@pytest.fixture
def plan(monkeypatch):
    data = json.dumps({"_allData": [{"_id": i} for i in (10000, 10001, 10946, 11198)]}).encode()
    monkeypatch.setattr(story_site, "master_index", lambda: (
        "https://master.example/", {"files": {"MasterAdv.json": hashlib.sha256(data).hexdigest()}}))
    monkeypatch.setattr(story_site, "get", lambda url: data)
    monkeypatch.setenv("MASTERDATA_REGION", "jp")
    monkeypatch.setenv("STORY_S3_PREFIX", "jp")
    monkeypatch.setenv("STORY_S3_PREFIX_SHARED", "")
    monkeypatch.setenv("STORY_LIMIT", "40")
    monkeypatch.setenv("REQUESTED", "")
    monkeypatch.setenv("FORCE", "false")
    prefixes, outputs, summaries = [], {}, []

    def run(published):
        class Bucket:
            def __init__(self, prefix=None):
                self.prefix = story_site.env("STORY_S3_PREFIX", "") if prefix is None else prefix
                prefixes.append(self.prefix)

            def published_story_ids(self):
                ids = published[self.prefix]
                if isinstance(ids, Exception):
                    raise ids
                return set(ids)

        monkeypatch.setattr(story_site, "Bucket", Bucket)
        monkeypatch.setattr(story_site, "output", outputs.__setitem__)
        monkeypatch.setattr(story_site, "summary", summaries.append)
        story_site.cmd_plan()
        return SimpleNamespace(prefixes=prefixes, outputs=outputs, summary="\n".join(summaries))

    return run


def test_jp_builds_only_unpublished_stories(plan):
    result = plan({"jp": [], "": [10000, 10001]})
    assert result.outputs == {"stories": "10946 11198", "count": "2"}
    assert "reused from the international site: 2" in result.summary
    assert "missing: 2" in result.summary


def test_jp_combines_both_sites_before_applying_limit(plan, monkeypatch):
    monkeypatch.setenv("STORY_LIMIT", "1")
    result = plan({"jp": [10946], "": [10000]})
    assert result.outputs == {"stories": "10001", "count": "1"}
    assert "missing: 2" in result.summary


def test_jp_ends_at_plan_when_every_story_is_available(plan):
    result = plan({"jp": [10946, 11198], "": [10000, 10001, 10946]})
    assert result.outputs == {"stories": "", "count": "0"}
    assert "reused from the international site: 2" in result.summary


def test_jp_checks_custom_international_prefix(plan, monkeypatch):
    monkeypatch.setenv("STORY_S3_PREFIX_SHARED", "international")
    result = plan({"jp": [], "international": [10000, 10001]})
    assert result.prefixes == ["jp", "international"]
    assert result.outputs["stories"] == "10946 11198"


@pytest.mark.parametrize("force,expected", [("false", "10946"), ("true", "10000 10001 10946")])
def test_requested_stories_reuse_existing_unless_forced(plan, monkeypatch, force, expected):
    monkeypatch.setenv("REQUESTED", "10000,10001,10946")
    monkeypatch.setenv("FORCE", force)
    result = plan({"jp": [10001], "": [10000]})
    assert result.outputs["stories"] == expected


def test_requested_unknown_id_is_rejected(plan, monkeypatch):
    monkeypatch.setenv("REQUESTED", "12345")
    with pytest.raises(SystemExit, match="no MasterAdv row for 12345"):
        plan({"jp": [], "": []})


def test_failed_international_index_does_not_trigger_full_jp_rebuild(plan):
    with pytest.raises(RuntimeError, match="unavailable"):
        plan({"jp": [], "": RuntimeError("unavailable")})


def test_international_plan_does_not_use_jp(plan, monkeypatch):
    monkeypatch.setenv("MASTERDATA_REGION", "hk-tw-mo")
    monkeypatch.setenv("STORY_S3_PREFIX", "")
    result = plan({"": [10000, 10001]})
    assert result.prefixes == [""]
    assert result.outputs["stories"] == "10946 11198"
    assert "reused" not in result.summary


@pytest.mark.parametrize("prefix", ["", "jp", "/international/"])
def test_asset_size_uses_head_on_its_own_site(monkeypatch, prefix):
    import boto3

    monkeypatch.setenv("STORY_S3_ENDPOINT", "https://storage.example")
    monkeypatch.setenv("STORY_S3_BUCKET", "stories")
    monkeypatch.setenv("STORY_S3_PREFIX", "unused")
    def head_object(**kwargs):
        normalized = prefix.strip("/")
        assert kwargs == {"Bucket": "stories", "Key": (normalized + "/" if normalized else "") + "assets/a.gz"}
        return {"ContentLength": 100}

    client = SimpleNamespace(head_object=head_object)
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)
    assert story_site.Bucket(prefix=prefix).object_size("assets/a.gz") == 100


def test_shared_story_ids_use_public_index_and_custom_prefix(monkeypatch):
    monkeypatch.setenv("STORY_S3_ENDPOINT", "https://storage.example/")
    bucket = story_site.Bucket.__new__(story_site.Bucket)
    bucket.name, bucket.prefix = "stories", "international/"

    def get(url, timeout):
        assert url == "https://storage.example/stories/international/stories.json"
        return json.dumps({"stories": [{"advId": 10000}, {"advId": 10001}]}).encode()

    monkeypatch.setattr(story_site, "get", get)
    assert bucket.published_story_ids() == {10000, 10001}


@pytest.mark.parametrize("status", [404, 403, 503])
def test_unpublished_index_is_empty_but_other_http_errors_stop_plan(monkeypatch, status):
    monkeypatch.setenv("STORY_S3_ENDPOINT", "https://storage.example")
    bucket = story_site.Bucket.__new__(story_site.Bucket)
    bucket.name, bucket.prefix = "stories", ""

    def get(url, timeout):
        raise story_site.urllib.error.HTTPError(url, status, "error", None, None)

    monkeypatch.setattr(story_site, "get", get)
    if status == 404:
        assert bucket.published_story_ids() == set()
    else:
        with pytest.raises(RuntimeError, match=f"stories.json: HTTP {status}"):
            bucket.published_story_ids()


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "AccessDenied", "InternalError"])
def test_head_treats_only_missing_objects_as_absent(monkeypatch, code):
    from botocore.exceptions import ClientError

    bucket = story_site.Bucket.__new__(story_site.Bucket)
    bucket.name, bucket.prefix = "stories", "jp/"

    def head_object(**kwargs):
        raise ClientError({"Error": {"Code": code}}, "HeadObject")

    bucket.s3 = SimpleNamespace(head_object=head_object)
    if code in ("404", "NoSuchKey"):
        assert bucket.object_size("assets/a.gz") is None
    else:
        with pytest.raises(ClientError):
            bucket.object_size("assets/a.gz")


@pytest.fixture
def public_site(monkeypatch):
    objects, reads = {}, []

    class Bucket:
        def read(self, path, *, missing_ok=False):
            reads.append(path)
            if path not in objects and not missing_ok:
                raise RuntimeError(f"cannot fetch {path}: HTTP 404")
            return objects.get(path)

    monkeypatch.setattr(story_site, "Bucket", Bucket)
    return objects, reads


def test_new_site_fetch_starts_empty(tmp_path, public_site):
    site = tmp_path / "work" / "site"
    story_site.cmd_fetch(str(site))
    assert (site / "assets").is_dir()
    assert json.loads(story_site.fetched_file(site).read_text()) == {}
    assert public_site[1] == list(story_site.INDEXES)


def test_fetch_follows_all_three_indexes_and_leaves_assets_remote(tmp_path, public_site):
    objects, reads = public_site
    for kind, manifest in [("stories", "stories/10946.json"), ("models", "models/a.json"),
                           ("charts", "charts/tw/1_expert.json")]:
        objects[f"{kind}.json"] = json.dumps({kind: [{"manifest": manifest}]}).encode()
        objects[manifest] = b'{"files": {"example": {"asset": "assets/a.gz"}}}'
    site = tmp_path / "site"
    story_site.cmd_fetch(str(site))
    before = json.loads(story_site.fetched_file(site).read_text())
    assert set(before) == set(objects)
    assert set(reads) == set(objects)
    assert all(before[path] == hashlib.sha256(data).hexdigest() for path, data in objects.items())
    assert not list((site / "assets").iterdir())


@pytest.mark.parametrize("manifest", ["jp/stories/10946.json", "../stories/a.json", "stories/../a.json",
                                      "stories/a\\b.json", "https://example/story.json"])
def test_fetch_rejects_manifest_outside_its_index_directory(tmp_path, public_site, manifest):
    public_site[0]["stories.json"] = json.dumps({"stories": [{"manifest": manifest}]}).encode()
    with pytest.raises(SystemExit, match="unexpected manifest path"):
        story_site.cmd_fetch(str(tmp_path / "site"))


def test_fetch_does_not_ignore_missing_advertised_manifest(tmp_path, public_site):
    public_site[0]["stories.json"] = b'{"stories": [{"manifest": "stories/10946.json"}]}'
    site = tmp_path / "site"
    with pytest.raises(RuntimeError, match="stories/10946.json: HTTP 404"):
        story_site.cmd_fetch(str(site))
    assert not story_site.fetched_file(site).exists()


def test_publish_skips_existing_assets_and_uploads_indexes_last(tmp_path, monkeypatch):
    site = tmp_path / "site"
    files = {"assets/existing.gz": b"same", "assets/new.gz": b"new", "assets/changed.gz": b"longer",
             "stories/10946.json": b"story", "stories.json": b"index", "models/a.json": b"unchanged"}
    for path, data in files.items():
        dest = site / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    story_site.fetched_file(site).write_text(json.dumps({"models/a.json": story_site.sha256(site / "models/a.json")}))
    uploaded, checked = [], []

    class Bucket:
        writable = True

        def object_size(self, path):
            checked.append(path)
            return {"assets/existing.gz": 4, "assets/changed.gz": 2}.get(path)

        def upload(self, path, src):
            uploaded.append(path)

    monkeypatch.setattr(story_site, "Bucket", Bucket)
    story_site.cmd_publish(str(site))
    assert set(checked) == {"assets/existing.gz", "assets/new.gz", "assets/changed.gz"}
    assert set(uploaded[:2]) == {"assets/new.gz", "assets/changed.gz"}
    assert uploaded[2:] == ["stories/10946.json", "stories.json"]


def test_jp_api_denial_stops_before_any_build_workers(tmp_path, monkeypatch):
    monkeypatch.setenv("MASTERDATA_REGION", "jp")
    monkeypatch.setattr(story_site, "configure_region", lambda: None)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[2:] == ["nnnotes", "master", "version"]
        assert kwargs["timeout"] == 60
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(story_site.subprocess, "run", run)
    with pytest.raises(SystemExit, match="JP Version preflight failed before starting workers"):
        story_site.cmd_build(str(tmp_path / "site"), ["10946", "10947"])
    assert len(calls) == 1
    assert not (tmp_path / "site.build.json").exists()


@pytest.mark.parametrize("region", ["jp", "hk-tw-mo"])
def test_build_runs_after_successful_jp_preflight_and_international_needs_none(tmp_path, monkeypatch, region):
    monkeypatch.setenv("MASTERDATA_REGION", region)
    monkeypatch.setattr(story_site, "configure_region", lambda: None)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(story_site.subprocess, "run", run)
    story_site.cmd_build(str(tmp_path / "site"), ["10946"])
    if region == "jp":
        assert calls.pop(0)[-2:] == ["master", "version"]
    assert len(calls) == 2
    assert "--story" in calls[0] and calls[1][-1] == "--player-only"
