"""Story workflow planning: reuse international advIds and keep nested sites out of bucket listings."""
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

            def keys(self, sub):
                assert sub == "stories/"
                ids = self.published_story_ids()
                return {f"stories/{i}.json": 100 for i in ids} | {"stories/readme.txt": 20}

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


@pytest.mark.parametrize("prefix,jp_prefix,keys,expected", [
    ("", "jp", ["stories/10000.json", "jp/stories/10946.json", "jp/assets/a.gz"], ["stories/10000.json"]),
    ("jp", "jp", ["jp/stories/10946.json", "jp/assets/a.gz"], ["stories/10946.json", "assets/a.gz"]),
    ("site", "site/japan", ["site/stories/10000.json", "site/japan/models/a.json"], ["stories/10000.json"]),
    ("international", "japan", ["international/stories/10000.json"], ["stories/10000.json"]),
])
def test_bucket_listing_excludes_nested_site(monkeypatch, prefix, jp_prefix, keys, expected):
    import boto3

    monkeypatch.setenv("STORY_S3_ENDPOINT", "https://storage.example")
    monkeypatch.setenv("STORY_S3_BUCKET", "stories")
    monkeypatch.setenv("STORY_S3_PREFIX", "unused")
    monkeypatch.setenv("STORY_S3_PREFIX_JP", jp_prefix)

    def paginate(**kwargs):
        assert kwargs == {"Bucket": "stories", "Prefix": prefix + "/" if prefix else ""}
        return [{"Contents": [{"Key": k, "Size": 100} for k in keys]}]

    client = SimpleNamespace(get_paginator=lambda name: SimpleNamespace(paginate=paginate))
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)
    assert list(story_site.Bucket(prefix=prefix).keys()) == expected


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
        with pytest.raises(story_site.urllib.error.HTTPError):
            bucket.published_story_ids()
