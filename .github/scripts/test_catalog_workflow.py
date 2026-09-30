"""Versioned catalogs must reach asset preflight and spawned story/Live2D workers."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
import story_site
import music_data
import synth

MODEL = "Character/Live2D/test/new/model/new"
EPISODE = "Adv/Episode/event_new/event_new"


@pytest.fixture
def publication(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("NNNOTES_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "user"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MASTERDATA_REGION", "hk-tw-mo")
    monkeypatch.setenv("NNNOTES_CATALOG_REGION", "tw")
    monkeypatch.setenv("NNNOTES_CATALOG_LANGUAGE", "zh-Hant")
    monkeypatch.setenv("NNNOTES_PATHS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("NNNOTES_SERVERS_TW_CDN", "https://stale.example")
    master = tmp_path / "master"
    master.mkdir()
    monkeypatch.setenv("NNNOTES_PATHS_MASTER", str(master))
    snapshot = {"region": "hk-tw-mo", "entry": {"version": "master-v1", "resource_version": "1.0.0.201",
                "server": {"cdnRoot": "https://cdn.example/prod/tw|https://backup.example/prod/tw"}}, "files": {}}
    path = story_site.snapshot_file(master)
    path.write_text(json.dumps(snapshot))
    catalog = synth.CatalogWriter().build([(MODEL, "Assets/new.prefab", []), (EPISODE, "Assets/new.asset", [])])
    return snapshot, path, catalog


def test_saved_resource_version_reaches_preflight_and_child(publication, monkeypatch):
    from nnnotes.catalog import Catalog
    from nnnotes.cli import open_catalog
    from nnnotes.config import Config
    from nnnotes.webmodel import catalog_models

    snapshot, path, data = publication
    cache = Path(os.environ["NNNOTES_PATHS_CACHE"])
    cache.mkdir()
    Catalog.cache_file("zh-Hant", cache).write_bytes(synth.CatalogWriter().build([("Old", "Assets/old", [])]))
    monkeypatch.setenv("NNNOTES_SERVERS_TW_CATALOG", str(cache / "catalog_main_zh-Hant.bin"))
    monkeypatch.setattr(story_site, "master_index", lambda: pytest.fail("build fetched a newer master index"))
    calls = []

    def get(url, *args):
        calls.append(url)
        return data

    monkeypatch.setattr(story_site, "get", get)
    story_site.configure_region()
    assert calls == ["https://cdn.example/prod/tw/asset/Android/catalog_1.0.0.201_zh-Hant.bin"]
    assert Path(os.environ["NNNOTES_SERVERS_TW_CATALOG"]).read_bytes() == data
    cfg = Config.load()
    catalog = open_catalog(cfg, bundles=False)
    assert catalog_models(catalog) == {"new": MODEL}
    rows = [{"_id": 1, "_advEpisodeAsset": "event_new"}]
    assert story_site.partition_story_assets([1], rows, catalog.has) == ([1], [])
    child = subprocess.run([sys.executable, "-c", "from nnnotes.config import Config; "
                            "from nnnotes.cli import open_catalog; "
                            "from nnnotes.webmodel import catalog_models; "
                            "c=open_catalog(Config.load(), bundles=False); "
                            "assert 'new' in catalog_models(c); print(Config.load().cdn('tw'))"],
                           capture_output=True, text=True, check=True)
    assert child.stdout.strip() == "https://cdn.example/prod/tw"
    previous = os.environ["NNNOTES_SERVERS_TW_CATALOG"]
    # A different publication cannot hit the old main/version cache.
    snapshot["entry"]["resource_version"] = "1.0.0.202"
    path.write_text(json.dumps(snapshot))
    story_site.configure_region()
    assert os.environ["NNNOTES_SERVERS_TW_CATALOG"] != previous
    assert calls[-1].endswith("catalog_1.0.0.202_zh-Hant.bin")


@pytest.mark.parametrize("region,name,language", [("hk-tw-mo", "tw", "zh-Hant"), ("en", "en", "en"), ("kr", "kr", "ko")])
def test_mirror_fallback_uses_matching_root(publication, monkeypatch, region, name, language):
    snapshot, path, data = publication
    monkeypatch.setenv("MASTERDATA_REGION", region)
    monkeypatch.setenv("NNNOTES_CATALOG_REGION", name)
    monkeypatch.setenv("NNNOTES_CATALOG_LANGUAGE", language)
    snapshot["region"] = region
    path.write_text(json.dumps(snapshot))
    calls = []

    def get(url, *args):
        calls.append(url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(url, 404, "missing", None, None)
        return data

    monkeypatch.setattr(story_site, "get", get)
    story_site.configure_region()
    assert len(calls) == 2 and all(url.endswith(f"catalog_1.0.0.201_{language}.bin") for url in calls)
    assert os.environ[f"NNNOTES_SERVERS_{name.upper()}_CDN"] == "https://backup.example/prod/tw"


@pytest.mark.parametrize("response", [None, b"not a catalog"])
def test_bad_catalog_stops_without_main_fallback(publication, monkeypatch, response):
    calls = []

    def get(url, *args):
        calls.append(url)
        if response is None:
            raise urllib.error.HTTPError(url, 404, "missing", None, None)
        return response

    monkeypatch.setattr(story_site, "get", get)
    with pytest.raises(SystemExit, match="refusing catalog_main fallback"):
        story_site.configure_region()
    assert len(calls) == 2 and all("catalog_main" not in url for url in calls)
    assert "NNNOTES_SERVERS_TW_CATALOG" not in os.environ


@pytest.mark.parametrize("version", [None, "", "../bad", "1/2", "v%2f", "v?x"])
def test_invalid_resource_version_fails_before_download(publication, monkeypatch, version):
    snapshot, path, _ = publication
    snapshot["entry"]["resource_version"] = version
    path.write_text(json.dumps(snapshot))
    monkeypatch.setattr(story_site, "get", lambda *args: pytest.fail("invalid version was downloaded"))
    with pytest.raises(SystemExit, match="resource_version"):
        story_site.configure_region()


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_cross_region_snapshot_is_rejected(publication, monkeypatch, missing):
    snapshot, path, _ = publication
    if missing:
        path.unlink()
    else:
        snapshot["region"] = "jp"
        path.write_text(json.dumps(snapshot))
    monkeypatch.setattr(story_site, "master_index", lambda: pytest.fail("silently fetched live metadata"))
    with pytest.raises(SystemExit, match="snapshot"):
        story_site.configure_region()


def test_jp_uses_saved_endpoints_without_international_catalog(publication, monkeypatch):
    snapshot, path, _ = publication
    monkeypatch.setenv("MASTERDATA_REGION", "jp")
    monkeypatch.setenv("NNNOTES_CATALOG_REGION", "jp")
    snapshot["region"] = "jp"
    snapshot["entry"] = {"client_version": "1.0.4", "upstream": {
        "api_root": "https://api.example", "cdn_root": "https://jp.example"}}
    path.write_text(json.dumps(snapshot))
    monkeypatch.setattr(story_site, "master_index", lambda: pytest.fail("read newer endpoints"))
    monkeypatch.setattr(story_site, "get", lambda *args: pytest.fail("JP used international catalog URL"))
    story_site.configure_region()
    assert os.environ["NNNOTES_SERVERS_JP_API"] == "https://api.example"
    assert os.environ["NNNOTES_SERVERS_JP_CDN"] == "https://jp.example"
    assert os.environ["NNNOTES_SERVERS_JP_CLIENT_VERSION"] == "1.0.4"
    assert "NNNOTES_SERVERS_JP_CATALOG" not in os.environ


@pytest.mark.parametrize("root", ["", "http://cdn.example", "https://user:pass@cdn.example",
                                 "https://cdn.example/../x", "https://cdn.example/%2f", "https://cdn.example?x"])
def test_invalid_cdn_stops_before_download(publication, monkeypatch, root):
    snapshot, path, _ = publication
    snapshot["entry"]["server"]["cdnRoot"] = root
    path.write_text(json.dumps(snapshot))
    monkeypatch.setattr(story_site, "get", lambda *args: pytest.fail("invalid root was used"))
    with pytest.raises(SystemExit, match="cdnRoot"):
        story_site.configure_region()


@pytest.mark.parametrize("module", [story_site, music_data])
def test_master_step_keeps_endpoints_and_resource_version(tmp_path, monkeypatch, module):
    manifest = json.dumps({"version": "master-v1", "files": []}).encode()
    digest = hashlib.sha256(manifest).hexdigest()
    entry = {"version": "master-v1", "resource_version": "1.0.0.201", "manifest_sha256": digest,
             "server": {"cdnRoot": "https://cdn.example/prod/tw"}}
    monkeypatch.setenv("MASTERDATA_REGION", "hk-tw-mo")
    monkeypatch.setattr(story_site, "master_index", lambda: ("https://metadata.example/master/", {
        "entry": entry, "files": {"MasterManifest.json": digest}}))
    monkeypatch.setattr(story_site, "get", lambda *args: manifest)
    master = tmp_path / "master"
    module.cmd_master(str(master))
    saved = json.loads(story_site.snapshot_file(master).read_text())
    assert saved["entry"]["server"] == entry["server"]
    assert saved["entry"]["resource_version"] == entry["resource_version"]
    # Failed re-download cannot leave an old receipt available to a later build.
    monkeypatch.setattr(story_site, "get", lambda *args: b"wrong hash")
    with pytest.raises(SystemExit, match="SHA-256"):
        module.cmd_master(str(master))
    assert not story_site.snapshot_file(master).exists()
