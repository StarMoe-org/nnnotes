"""Self-test of the music data gates (music_data.py): a synthetic music-data.json passes every gate, and each gate
fails on the defect it is there for. Runs in seconds, without the network:

    python -m pytest -q -p no:cacheprovider .github/scripts/test_music_data.py

The JSON Schema gate uses docs/schema/music-data.schema.json (or $MUSIC_DATA_SCHEMA) when the checkout has it; the
page smoke test runs when $MUSIC_DATA_PAGE names ournotes-player's examples/songs (and Node.js is installed). Real
files, when named: $MUSIC_DATA_SAMPLE (a current nnnotes.music-data/2 file: the content gates pass) and
$MUSIC_DATA_OLD_SAMPLE (a legacy sampled file: the deck gate rejects its missing nominal expectations).
"""
import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import music_data                                   # noqa: E402
from music_data import Context, gates               # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
LANGS = ["ja", "en", "zh-Hant", "zh-Hans", "ko"]
TABLES = music_data.SONG_TABLES + ("MasterLiveSkillEffect", "MasterLiveGekisouRankingScoreBonus")
BIN = {t: hashlib.sha256(t.encode()).hexdigest() for t in TABLES}      # the manifest's hashes of the served files
DECK = "d4" * 20
CONTENT = ("deck", "scenarios", "finite", "references", "bgm")


def schema_path():
    p = Path(os.environ.get("MUSIC_DATA_SCHEMA") or ROOT / music_data.SCHEMA)
    if not p.is_file():
        return None
    return p if importlib.util.find_spec("jsonschema") else None


def page_path():
    p = os.environ.get("MUSIC_DATA_PAGE")
    return Path(p) if p and Path(p, "ranking.js").is_file() and shutil.which("node") else None


# ---------------------------------------------------------------- a synthetic file
def text(stem):
    return {lang: f"{stem}-{lang}" for lang in LANGS}


def check_deck(exact):
    return {"deck": [[0, 5000], None], "exact": exact, "predicted": exact + 0.25, "bound": 7.0}


LUCK_SEEDS = __import__("nnnotes.musicdata", fromlist=["published_seeds"]).published_seeds(2)                                                   # a luck chart's seeds; else the one seed 0
# the Gekisou skill aptitude's shapes: id, source, mission, band condition
SHAPES = [(0, "member", 1, False), (1, "support", 2, True), (2, "member", 3, False), (3, "support", 4, False)]


def effect(band=False):
    return {"effectType": 2000, "triggerType": 7010, "activationTimeSecond": 5.0, "effectValue": 1000,
            "maxEffectValue": 0, "effectLimitCount": 0, "effectExecuteLimitCount": 0, "skillTargetIds": [],
            "trigger": [[{"type": 7010, "values": [1], "positive": True, "targetIds": []}]],
            "condition": [[{"type": 5000, "values": [1], "positive": True, "targetIds": None}]] if band else [],
            "release": [], "reset": [], "cumulative": None}


def aptitude_head():
    return {"plainKind": 0, "host": "one performer with a synthetic empty Gekisou skill", "law": "independent nominal lottery and skill probabilities",
            "shapes": [{"id": i, "source": src, "mission": m, "bandCondition": band, "effects": [effect(band)],
                        "skills": [{"id": 10 + i, "level": 5, "memberTargetIds": [41] if band else None,
                                    "bandIds": [1] if band else None}]} for i, src, m, band in SHAPES]}


def variant(deck, shape, match):
    """A complete single-shape expectation with an independently reconstructible check."""
    def p(value):
        return [value, 0]
    ranges = [{"rangeScore": p(300), "rankBonus": p(750), "rankBonusPerfect": p(750),
               "rangeScorePerfect": p(300), "maxCombo": p(0), "justCount": p(0), "luckPoints": p(2)}
              for _ in deck["ranges"]]
    base = deck["expectation"]
    linear = base["rangeWeights"] is not None
    result = {"shape": shape, "bandMatch": match, "score": p(1500), "scorePerfect": p(1500),
              "tail": p(1500 - 1050 * len(ranges)), "tailPerfect": p(1500 - 1050 * len(ranges)),
              "converted": p(0), "ranges": ranges, "weights": [p(0.01), p(0.02)],
              "rangeWeights": [[p(0.001)] * len(ranges), [p(0.002)] * len(ranges)] if linear else None,
              "check": {"ranks": [2] * len(ranges) if linear else [1] * len(ranges),
                        "deck": [[0, 7000], None], "bound": 3.0}}
    score = base["score"][0] + 1500
    weight = base["weights"][0][0][0] + 0.01
    if linear:
        for r, info in zip(base["ranges"], deck["ranges"], strict=True):
            percent = info["rankBonusPercents"][1]
            score += r["rangeScore"][0] * percent / 100 - r["rankBonus"][0] + 300 * (percent - 250) / 100
        weight += (190 - 250) / 100 * (0.1 + 0.001)
    predicted = 1000003 * (score / 300000 + 0.7 * weight)
    result["check"].update(expected=[predicted, 0], predicted=[predicted, 0])
    return result


def aptitude(deck, luck):
    missions = {r["mission"] for r in deck["ranges"]}
    variants = [variant(deck, i, match) for i, _, m, band in SHAPES
                if m == 4 or m in missions for match in ((True, False) if band else (None,))]
    factors = [{"judgedNotes": 12, "justNotes": 0, "perfectNotes": 0, "tailNotes": 2, "comboAtStart": 5,
                "lotteries": [4.0, 0.0] if r["mission"] == 2 else [0, 0]} for r in deck["ranges"]]
    return {"factors": factors, "variants": variants}


def deck_chart(luck=False):
    def p(value):
        return [value, 0]
    def checked(value, ranks=None):
        return {"deck": [[0, 5000], None], "ranks": ranks, "expected": p(value),
                "predicted": [value + 0.25, 0], "bound": 7.0}
    expected = {"score": p(120000), "scorePerfect": p(120000),
                "ranges": [{"rangeScore": p(4000), "rankBonus": p(10000), "rankBonusPerfect": p(10000),
                            "rangeScorePerfect": p(4000), "maxCombo": 10, "justCount": 0,
                            "luckPoints": p(3 if luck else 0),
                            "lotResults": list(map(p, [1, 2, 0, 1] if luck else [0, 0, 0, 0]))}],
                "weights": [[p(0.5), p(0.25)]], "rangeWeights": [[[p(0.1)], [p(0.05)]]],
                "check": checked(2000), "rankCheck": checked(1900, [3])}
    d = {"convertedNoteCount": 20, "skip": 0.01, "events": [[0, 1000], [1, 3000]], "positions": 2,
         "ranges": [{"index": 0, "mission": 2 if luck else 1, "startMs": 1000, "endMs": 5000,
                     "rankBonusPercent": 250, "rankBonusPercents": [250, 190, 160, 100, 100]}],
         "justNotes": 0, "expectation": expected, "replaySeeds": LUCK_SEEDS if luck else [0],
         "offSeeds": [{"seed": 0, "score": 90000, "weights": [[0.4, 0.2]], "check": check_deck(1500)}],
         "unplayable": None}
    d["gekisouAptitude"] = aptitude(d, luck)
    return d


def chart(difficulty, score_id, last=60000, luck=False):
    return {"difficulty": difficulty, "scoreId": score_id, "level": 10, "displayLevel": 10.5, "fullComboCount": 20,
            "asset": {"key": f"Live/MusicScore/c/c_{score_id}", "sha256": "ab" * 32},
            "notes": {"judged": 20, "total": 22, "byOperateType": {"1": 20, "120": 2}},
            "bpm": {"main": 120.0, "min": 120.0, "max": 120.0, "changes": [{"timeMs": 0, "bpm": 120.0}]},
            "firstNoteMs": 1000, "lastJudgedNoteMs": last, "lastNoteMs": last, "musicLengthMs": last + 1000,
            "skillEventsMs": [1000, 3000], "fevers": [[1000, 5000]], "deck": deck_chart(luck)}


def song(i, charts):
    ranks = ["D", "C", "B", "A", "S", "SS"]
    return {"id": i, "sortOrder": i, "startAt": "2026/01/01 0:00:00", "defaultUnlock": True, "title": text(f"t{i}"),
            "ruby": None, "phonetic": text(f"p{i}"), "bandIds": [1], "bandName": None, "vocalCharacterIds": [1],
            "lyricist": text("l"), "composer": text("c"), "arranger": None, "musicType": 1, "musicCategories": [1],
            "bestMusicTagIds": [1], "jacket": f"jkt_{i}", "gekisouMissions": [1, 2, 3],
            "bgm": {"soundId": i, "cueSheet": f"Bgm{i}", "cue": f"song{i}",
                    "length": {"lengthMs": 90000, "samples": 48000 * 90, "sampleRate": 48000, "durationMs": 90000}},
            "scoreRanks": [{"rank": r, "requiredScore": n * 1000, "battleRequiredScore": n * 2000}
                           for n, r in enumerate(ranks)],
            "charts": charts, "master": {"MasterLiveMusic": {"_id": i, "_rate": 1.5}, "MasterLiveScoreRank": []}}


def sample() -> dict:
    kind = {"id": 0, "effectType": 2000, "activationTimeSecond": 5.0, "durationMs": 5000, "skillTargetIds": [],
            "skillConditionGroup": 0, "skillReleaseConditionGroup": 0, "effectLimitCount": 0,
            "effectExecuteLimitCount": 0, "effectExecuteLimitResetConditionGroup": 0, "rows": 1, "values": [10000]}
    return {
        "format": "nnnotes.music-data/2",
        "provenance": {
            "region": "tw", "client": {"versionName": "1.0.1", "versionCode": 25},
            "catalog": {"resourceVersion": None, "sha256": "cd" * 32},
            "master": {"source": "api", "version": "v-test", "tables": {t: {"sha256": BIN[t]} for t in TABLES}},
            "exporter": {"name": "nnnotes", "version": "0.1.2", "chartFormat": "nnnotes.live-score/1"},
            "deck": {"name": "ournotes-deck", "version": "0.0.1",
                     "source": "https://github.com/empty-sekai/ournotes-deck", "commit": DECK, "sourceSha256": "de" * 32,
                     "format": "ournotes-deck.chart-stats/3"}},
        "languages": LANGS,
        "bands": [{"id": 1, "name": text("band"), "mainColor": "#3388BB", "subColor": "#FFFFFF"}],
        "characters": [{"id": 1, "bandId": 1, "name": text("ch"), "shortName": text("c"), "mainColor": "#77BBDD"}],
        "tags": [{"id": 1, "name": text("tag")}],
        "categories": [{"id": 1, "musicCategories": [1], "name": text("cat")}],
        "deck": {"model": {"power": 300000, "checkPower": 1000003, "gekisouAptitude": "one skill at a time"},
                 "kinds": [kind], "gekisouAptitude": aptitude_head()},
        "songs": [song(100001, [chart("easy", 10), chart("expert", 30, luck=True)]),
                  song(100002, [chart("expert", 40, luck=True)])],
    }


def context(tmp_path: Path, doc: dict, **kw) -> tuple[bytes, Context]:
    """The file's bytes and what check reads next to it: the decoded master data and its snapshot, the jackets (the
    page smoke test only with page=page_path(): it starts Node.js)."""
    master = tmp_path / "master"
    master.mkdir(exist_ok=True)
    files = {}
    for t in TABLES:
        rows = []
        if t == "MasterLiveMusic":
            rows = [{"_id": s["id"], "_liveScoreRankGroup": s["id"]} for s in doc.get("songs") or []]
        elif t == "MasterLiveScoreRank":
            names = {"E": 1, "D": 2, "C": 3, "B": 4, "A": 5, "S": 6, "SS": 7}
            rows = [{"_id": s["id"] * 10 + i, "_group": s["id"], "_liveScoreRank": names[r["rank"]],
                     "_requiredScore": r["requiredScore"], "_battleLiveRequiredScore": r["battleRequiredScore"]}
                    for s in doc.get("songs") or [] for i, r in enumerate(s.get("scoreRanks") or [])]
        data = json.dumps({"_allData": rows}).encode()
        (master / f"{t}.json").write_bytes(data)
        files[f"{t}.json"] = hashlib.sha256(data).hexdigest()
    listed = [{"name": f"{t}.bin", "hash": BIN[t], "size": 1} for t in TABLES]
    (master / music_data.MANIFEST).write_text(json.dumps({"version": "v-test", "files": listed}), encoding="utf-8")
    files[music_data.MANIFEST] = hashlib.sha256((master / music_data.MANIFEST).read_bytes()).hexdigest()
    jackets = tmp_path / "jackets"
    jackets.mkdir(exist_ok=True)
    for s in doc.get("songs") or []:
        if s.get("jacket"):
            (jackets / f"{s['jacket']}.webp").write_bytes(b"RIFF....WEBP")
    # an infinity as nnnotes writes it (1e999: JSON that JavaScript reads too); a NaN stays NaN (nnnotes writes none)
    raw = json.dumps(doc, ensure_ascii=False).replace("Infinity", "1e999").encode("utf-8")
    (tmp_path / "music-data.json").write_bytes(raw)
    ctx = Context(region="tw", language="zh-Hant", master=master,
                  snapshot={"entry": {"version": "v-test", "client_version": "1.0.1"}, "files": files},
                  deck_commit=DECK, nnnotes_version="0.1.2", jackets=jackets, schema=schema_path(), published=None,
                  page=None, file=tmp_path / "music-data.json")
    for k, v in kw.items():
        setattr(ctx, k, v)
    return raw, ctx


def run(tmp_path, doc, **kw) -> dict:
    return gates(*context(tmp_path, doc, **kw))


def gate(report: dict, name: str) -> dict:
    return next(g for g in report["gates"] if g["gate"] == name)


def failures(report: dict) -> list:
    return [(g["gate"], g["failures"]) for g in report["gates"] if not g["passed"]]


def seed(doc, song=0, chart=0):
    return doc["songs"][song]["charts"][chart]["deck"]["expectation"]


def apt(doc, song=0, chart=0):
    return doc["songs"][song]["charts"][chart]["deck"]["gekisouAptitude"]


def var(doc, i=0, song=0, chart=0):
    return apt(doc, song, chart)["variants"][i]


def shape(doc, i):
    return doc["deck"]["gekisouAptitude"]["shapes"][i]


def nonlinear(doc, song=0, chart=0):
    """An overlapping-range chart has no linear response and checks rank 1."""
    d = doc["songs"][song]["charts"][chart]["deck"]
    d["expectation"].update(rangeWeights=None, rankCheck=None)
    for v in d["gekisouAptitude"]["variants"]:
        v["rangeWeights"] = None
        v["check"]["ranks"] = [1] * len(d["ranges"])
        predicted = 1000003 * ((d["expectation"]["score"][0] + v["score"][0]) / 300000 + 0.7 * 0.51)
        v["check"].update(expected=[predicted, 0], predicted=[predicted, 0])


# ---------------------------------------------------------------- the gates
def test_the_sample_passes_every_gate(tmp_path):
    r = run(tmp_path, sample(), page=page_path())
    assert r["passed"], failures(r)
    assert [g["gate"] for g in r["gates"]] == [name for name, _ in music_data.GATES]
    assert not [g for g in r["gates"] if g["warningCount"]]
    assert r["sha256"] == hashlib.sha256((tmp_path / "music-data.json").read_bytes()).hexdigest()


def test_schema(tmp_path):
    if schema_path() is None:
        pytest.skip("no docs/schema/music-data.schema.json (a fork not synced with upstream) or no jsonschema")
    doc = sample()
    doc["songs"][0]["charts"][0]["level"] = "10"
    g = gate(run(tmp_path, doc), "schema")
    assert not g["passed"] and any("songs/0/charts/0/level" in f for f in g["failures"])


def test_page_smoke(tmp_path):
    if page_path() is None:
        pytest.skip("set MUSIC_DATA_PAGE to ournotes-player's examples/songs (and install Node.js)")
    assert gate(run(tmp_path, sample(), page=page_path()), "page")["passed"]
    doc = sample()
    for s in doc["songs"]:
        for c in s["charts"]:
            c["deck"].pop("offSeeds")
    g = gate(run(tmp_path, doc, page=page_path()), "page")
    assert not g["passed"] and any("free scenario" in f for f in g["failures"])


def test_page_aptitude_check_reconstruction(tmp_path):
    page = page_path()
    if page is None or "aptitudeFigures" not in (page / "ranking.js").read_text(encoding="utf-8"):
        pytest.skip("page does not yet expose the aptitude API")
    doc = sample()
    # Replay seeds do not change any nominal figure or its independent expectation check.
    doc["songs"][0]["charts"][1]["deck"]["replaySeeds"] = []
    assert gate(run(tmp_path, doc, page=page), "page")["passed"]
    # Even a self-consistent exported check is rejected when the page reconstructs another expectation.
    var(doc)["check"].update(expected=[99999999, 0], predicted=[99999999, 0])
    g = gate(run(tmp_path, doc, page=page), "page")
    assert any("nominal check reconstruction" in f for f in g["failures"]), g


def unplayable(doc):
    doc["songs"][1]["charts"][0]["deck"]["unplayable"] = "more than three fevers"      # its seeds kept


@pytest.mark.parametrize("change, name, match", [
    (lambda d: d["songs"][0]["charts"][0]["deck"].pop("offSeeds"), "scenarios", "offSeeds missing"),
    (lambda d: d["songs"][0]["charts"][1]["deck"]["offSeeds"].append({}), "scenarios", "offSeeds missing"),
    (lambda d: d["songs"][0]["charts"][0]["deck"]["ranges"][0].pop("rankBonusPercents"), "scenarios", "not five ints"),
    (lambda d: d["songs"][0]["charts"][0]["deck"]["ranges"][0].update(rankBonusPercents=[251, 190, 160, 100, 100]), "scenarios", "is not rankBonusPercent"),
    (lambda d: seed(d).pop("scorePerfect"), "scenarios", "no scorePerfect"),
    (lambda d: seed(d).pop("rangeWeights"), "scenarios", "no rangeWeights"),
    (lambda d: seed(d).pop("rankCheck"), "scenarios", "no rankCheck"),
    (lambda d: seed(d)["ranges"][0].pop("rankBonusPerfect"), "deck", "outward radius"),
    (lambda d: seed(d).update(rangeWeights=[[[[0.1, 0]]]]), "deck", "rangeWeights are not"),
    (lambda d: seed(d)["rankCheck"].update(expected=[99999, 0]), "deck", "exceeds its bound"),
    (lambda d: d["deck"].pop("gekisouAptitude"), "aptitude", "gekisouAptitude missing"),
    (lambda d: d["deck"]["model"].pop("gekisouAptitude"), "aptitude", "no text"),
    (lambda d: d["deck"]["gekisouAptitude"].update(plainKind=1), "aptitude", "the page's plain kind is 0"),
    (lambda d: d["deck"]["gekisouAptitude"].pop("host"), "aptitude", "no host"),
    (lambda d: d["deck"]["gekisouAptitude"].update(law="sampled"), "aptitude", ".law"),
    (lambda d: shape(d, 1).update(id=5), "aptitude", "ids are not 0, 1, 2"),
    (lambda d: shape(d, 0).update(source="card"), "aptitude", "source 'card'"),
    (lambda d: shape(d, 2).update(mission=5), "aptitude", "mission 5"),
    (lambda d: shape(d, 1)["effects"][0]["condition"][0][0].update(targetIds=[41]), "aptitude", "targetIds not null"),
    (lambda d: shape(d, 0)["effects"][0].pop("cumulative"), "aptitude", "cumulative missing"),
    (lambda d: shape(d, 0)["effects"][0].update(effectValue=1.5), "aptitude", "effectValue missing"),
    (lambda d: shape(d, 0)["effects"][0].update(reset=None), "aptitude", "reset missing"),
    (lambda d: shape(d, 1)["skills"][0].update(memberTargetIds=None), "aptitude", "is not an id, a level"),
    (lambda d: shape(d, 0).update(skills=[]), "aptitude", "no skills"),
    (lambda d: d["songs"][0]["charts"][0]["deck"].update(gekisouAptitude=None), "aptitude", "none (gekisouAptitude"),
    (unplayable, "aptitude", "with a chart unplayable"),
    (lambda d: apt(d)["factors"].pop(), "aptitude", "0 factors for 1 ranges"),
    (lambda d: apt(d)["factors"][0].update(justNotes=3), "aptitude", "outside a Just"),
    (lambda d: apt(d)["factors"][0].update(tailNotes=-1), "aptitude", "not {"),
    (lambda d: apt(d, 0, 1)["factors"][0].update(lotteries=[3.0, 0]), "aptitude", "expected results"),
    (lambda d: apt(d)["variants"].pop(0), "aptitude", "variants (shape, bandMatch)"),
    (lambda d: apt(d)["variants"].reverse(), "aptitude", "variants (shape, bandMatch)"),
    (lambda d: var(d).update(shape=9), "aptitude", "variants (shape, bandMatch)"),
    (lambda d: var(d, 0, 0, 1).update(bandMatch=None), "aptitude", "variants (shape, bandMatch)"),
    (lambda d: var(d).pop("tail"), "aptitude", "tail None is not"),
    (lambda d: var(d).update(score=[1500]), "aptitude", "outward radius"),
    (lambda d: var(d).update(score=[1500, -1]), "aptitude", "outward radius"),
    (lambda d: var(d).update(converted=[float("nan"), 0]), "aptitude", "outward radius"),
    (lambda d: var(d).update(converted=[0, 0.1]), "aptitude", "deterministic integer"),
    (lambda d: var(d).update(tail=[451, 0]), "aptitude", "tail interval is inconsistent"),
    (lambda d: var(d).update(tailPerfect=[449, 0]), "aptitude", "tailPerfect interval is inconsistent"),
    (lambda d: var(d)["weights"].append([0, 0]), "aptitude", "weights are not"),
    (lambda d: seed(d).update(rangeWeights=None, rankCheck=None), "aptitude", "rank-linear domain"),
    (lambda d: var(d).update(rangeWeights=[[[0, 0]]]), "aptitude", "rangeWeights are not"),
    (lambda d: (nonlinear(d), var(d)["check"].update(ranks=[2])), "aptitude", "check ranks"),
    (lambda d: var(d)["check"].update(ranks=[6]), "aptitude", "check ranks"),
    (lambda d: var(d)["check"].update(deck=[[1, 7000], None]), "aptitude", "check deck"),
    (lambda d: var(d)["check"].update(expected=[99999999, 0]), "aptitude", "exceeds its bound"),
    (lambda d: var(d)["check"].pop("bound"), "aptitude", "expectation check"),
    (lambda d: d.update(deck=None), "deck", "deck is null"),
    (lambda d: d["songs"][0]["charts"][0].update(deck=None), "deck", "no deck statistics"),
    (lambda d: seed(d)["check"].update(expected=[99999, 0]), "deck", "exceeds its bound"),
    (lambda d: seed(d).update(weights=[[[0.5, 0]]]), "deck", "weights are not"),
    (lambda d: d["songs"][0]["charts"][0]["deck"].update(replaySeeds=[]), "deck", "nonempty integer"),
    (unplayable, "deck", "unplayable, but has replay seeds"),
    (lambda d: d["songs"][0]["charts"][0]["deck"].update(events=[[0, 1000]]), "deck", "skill events do not match"),
    (lambda d: d["songs"][0]["charts"][0]["deck"].update(replaySeeds=[5]), "deck", "must be [0]"),
    (lambda d: d["songs"][0]["charts"][1]["deck"].update(replaySeeds=[11, 22]), "deck", "published seed prefix"),
    (lambda d: seed(d)["ranges"][0].pop("luckPoints"), "deck", "outward radius"),
    (lambda d: seed(d)["ranges"][0].update(luckPoints=[2.5, 0]), "deck", "outside a luck range"),
    # numbers
    (lambda d: seed(d)["weights"][0].__setitem__(1, float("nan")), "finite", "weights[0][1]: not finite"),
    (lambda d: d["songs"][1]["charts"][0]["bpm"].update(main=float("inf")), "finite", "bpm.main: not finite"),
    # references and texts
    (lambda d: d["songs"][1]["bandIds"].append(9), "references", "band 9 not in"),
    (lambda d: d["songs"][1]["vocalCharacterIds"].append(7), "references", "character 7 not in"),
    (lambda d: d["songs"][0].update(title=None), "references", "title: no text"),
    (lambda d: d["songs"][0].update(title={lang: "" for lang in LANGS}), "references", "empty in every language"),
    (lambda d: d["songs"][0]["composer"].pop("ko"), "references", "composer: not a text"),
    (lambda d: d["songs"][0].update(jacket=None), "references", "no jacket"),
    (lambda d: d["songs"].reverse(), "references", "not sorted"),
    (lambda d: d["songs"][1]["charts"][0].update(scoreId=10), "references", "occurs twice"),
    (lambda d: d["songs"][0]["charts"].reverse(), "references", "charts ['expert', 'easy']"),
    (lambda d: d["songs"][0].update(scoreRanks=[]), "references", "score ranks"),
    (lambda d: d["songs"][0].update(bandIds=[]), "references", "neither a band nor a band name"),
    # BGM
    (lambda d: d["songs"][0]["bgm"].update(length=None), "bgm", "no BGM length"),
    (lambda d: d["songs"][0]["bgm"]["length"].update(durationMs=50000, samples=48000 * 50), "bgm",
     "ends before the last note"),
    (lambda d: d["songs"][0]["bgm"]["length"].update(durationMs=90500), "bgm", "is not samples"),
    (lambda d: d["songs"][0]["bgm"]["length"].update(lengthMs=95000), "bgm", "cue length"),
    (lambda d: d["songs"][0]["bgm"]["length"].update(durationMs=1200000, samples=48000 * 1200), "bgm",
     "bounds"),
    # provenance
    (lambda d: d.update(format="nnnotes.music-data/99"), "provenance", "format"),
    (lambda d: d["provenance"].update(region="en"), "provenance", "region 'en'"),
    (lambda d: d["provenance"]["master"].update(version="other"), "provenance", "the snapshot's 'v-test'"),
    (lambda d: d["provenance"]["master"].update(source="embedded"), "provenance", "master.source"),
    (lambda d: d["provenance"]["master"]["tables"]["MasterText"].update(sha256="00" * 32), "provenance",
     "MasterText.sha256 is not"),
    (lambda d: d["provenance"]["master"]["tables"].pop("MasterBand"), "provenance", "lacks MasterBand"),
    (lambda d: d["provenance"]["deck"].update(commit="ee" * 20), "provenance", "rust/Cargo.lock pins"),
    (lambda d: d["provenance"]["exporter"].update(version="0.0.9"), "provenance", "installed nnnotes"),
    (lambda d: d["provenance"]["client"].update(versionName=None), "provenance", "no APK version"),
])
def test_a_gate_fails(tmp_path, change, name, match):
    doc = sample()
    change(doc)
    r = run(tmp_path, doc)
    g = gate(r, name)
    assert not r["passed"] and not g["passed"] and any(match in f for f in g["failures"]), g


def test_a_table_read_is_not_the_listed_one(tmp_path):
    raw, ctx = context(tmp_path, sample())
    (ctx.master / "MasterText.json").write_text('{"_allData": [{}]}', encoding="utf-8")
    g = gate(gates(raw, ctx), "provenance")
    assert not g["passed"] and g["failures"] == ["MasterText.json read is not the one index.json lists"]


def test_a_jacket_file_is_missing(tmp_path):
    raw, ctx = context(tmp_path, sample())
    (ctx.jackets / "jkt_100002.webp").unlink()
    g = gate(gates(raw, ctx), "references")
    assert not g["passed"] and g["failures"] == ["song 100002: no jacket file jackets/jkt_100002.webp"]


def test_warnings_do_not_fail(tmp_path):
    doc = sample()
    nonlinear(doc)                                                        # overlapping ranges
    seed(doc, 0, 1)["rangeWeights"][0] = None
    seed(doc, 0, 1)["rankCheck"] = None                             # a kind reading the confirmed rank
    doc["songs"][1]["charts"][0]["deck"]["offSeeds"][0]["weights"][0] = None
    doc["songs"][1]["charts"][0]["deck"]["offSeeds"][0]["check"]["deck"] = [None, None]
    doc["songs"][0]["master"]["MasterLiveMusic"]["_rate"] = float("inf")  # 1e999 in master data as served
    doc["songs"][1]["title"]["zh-Hant"] = ""
    r = run(tmp_path, doc, page=page_path())
    assert r["passed"], failures(r)
    assert gate(r, "scenarios")["warningCount"] == 3 and gate(r, "finite")["warningCount"] == 1
    assert gate(r, "references")["warnings"] == ["songs without a zh-Hant title: 100002"]


def test_uncertainty_cannot_conceal_a_failed_expectation_check(tmp_path):
    doc = sample()
    var(doc, 0, 0, 1)["check"]["expected"][1] = 100
    r = run(tmp_path, doc)
    assert not r["passed"]
    assert any("exceeds its bound" in x for x in gate(r, "aptitude")["failures"])


def test_no_gekisou_skill_shapes(tmp_path):
    doc = sample()
    doc["deck"]["gekisouAptitude"]["shapes"] = []
    kept = apt(doc)
    for _, c in music_data.charts_of(doc):
        c["deck"]["gekisouAptitude"] = None
    r = run(tmp_path, doc, page=page_path())
    assert r["passed"], failures(r)
    doc["songs"][0]["charts"][0]["deck"]["gekisouAptitude"] = kept
    g = gate(run(tmp_path, doc), "aptitude")
    assert any("no shape" in x for x in g["failures"])


def test_an_unplayable_chart_has_no_aptitude(tmp_path):
    doc = sample()
    doc["songs"][1]["charts"][0]["deck"].update(unplayable="more than three fevers", replaySeeds=[], expectation=None, gekisouAptitude=None)
    r = run(tmp_path, doc, page=page_path())
    assert r["passed"], failures(r)
    assert gate(r, "aptitude")["note"].startswith("4 shapes, 2 charts")


def test_gzip_caps(tmp_path, monkeypatch):
    raw, ctx = context(tmp_path, sample())
    g = gate(gates(raw, ctx, only=("gzip",)), "gzip")
    assert g["passed"] and g["note"].startswith(f"{len(music_data.gzip.compress(raw, 6))} bytes gzipped")
    monkeypatch.setattr(music_data, "APTITUDE_GZIP_MAX", 100)
    g = gate(gates(raw, ctx, only=("gzip",)), "gzip")
    assert not g["passed"] and g["failures"][0].startswith("the Gekisou skill aptitude: ")
    monkeypatch.setattr(music_data, "FILE_GZIP_MAX", 100)
    assert len(gate(gates(raw, ctx, only=("gzip",)), "gzip")["failures"]) == 2


def test_against_the_published_file(tmp_path):
    doc = sample()
    raw, ctx = context(tmp_path, doc)
    more = copy.deepcopy(doc)
    more["songs"].append(song(100003, [chart("expert", 50)]))
    ctx.published = json.dumps(more).encode()
    r = gates(raw, ctx)
    assert not gate(r, "counts")["passed"] and gate(r, "counts")["failures"] == [
        "2 songs, the published file has 3", "3 charts, the published file has 4"]
    assert gate(r, "counts")["warnings"] == ["songs no longer in the file: 100003", "charts no longer in the file: 50"]
    ctx.published = raw + b" " * (len(raw) * 2)                           # the same songs in a file three times as big
    r = gates(raw, ctx)
    assert gate(r, "counts")["passed"] and not gate(r, "size")["passed"]
    ctx.published = raw[:len(raw) // 3]                                   # a third: not JSON, and twice exceeded
    r = gates(raw, ctx)
    assert gate(r, "counts")["warnings"] == ["the published music-data.json is not JSON: skipped"]
    assert not gate(r, "size")["passed"]
    ctx.published = raw
    assert gates(raw, ctx)["passed"]


def test_not_json(tmp_path):
    r = gates(b"{", Context())
    assert not r["passed"] and r["gates"][0]["gate"] == "json"


def test_report_markdown(tmp_path):
    doc = sample()
    doc["songs"][0]["charts"][0]["deck"].pop("offSeeds")
    text_ = music_data.report_markdown(run(tmp_path, doc))
    assert "| scenarios | **failed** (1) |" in text_
    assert "- scenarios failure: chart 10 (100001 easy): offSeeds missing or not exactly one entry" in text_


def test_deck_package_from_cargo_lock(tmp_path):
    lock = tmp_path / "rust" / "Cargo.lock"
    lock.parent.mkdir()
    lock.write_text('[[package]]\nname = "pyo3"\nversion = "0.29.0"\n\n[[package]]\nname = "ournotes-sim"\n'
                    'version = "0.0.1"\nsource = "git+https://github.com/empty-sekai/ournotes-deck?rev=' + "a" * 40
                    + "#" + "b" * 40 + '"\n', encoding="utf-8")
    assert music_data.deck_package(tmp_path) == ("0.0.1", "b" * 40)
    assert music_data.deck_commit(tmp_path) == "b" * 40
    with pytest.raises(SystemExit, match="sync the fork"):
        music_data.deck_commit(tmp_path / "nowhere")


# ---------------------------------------------------------------- real files (when named)
def real(name):
    p = os.environ.get(name)
    if not p or not Path(p).is_file():
        pytest.skip(f"set {name} to a music-data.json")
    return Path(p).read_bytes()


def test_a_real_legacy_file_is_rejected():
    raw = real("MUSIC_DATA_OLD_SAMPLE")
    assert json.loads(raw)["format"] == "nnnotes.music-data/1"
    r = gates(raw, Context(language="zh-Hant"), only=CONTENT)
    assert not gate(r, "deck")["passed"]
    assert any("expectation" in f or "replaySeeds" in f for f in gate(r, "deck")["failures"])


def test_a_real_file_with_the_scenario_fields(tmp_path):
    raw = real("MUSIC_DATA_SAMPLE")
    assert json.loads(raw)["format"] == "nnnotes.music-data/2"
    (tmp_path / "music-data.json").write_bytes(raw)
    ctx = Context(language="zh-Hant", page=page_path(), file=tmp_path / "music-data.json", published=raw)
    r = gates(raw, ctx, only=CONTENT + ("aptitude", "counts", "size", "gzip", "page"))
    assert r["passed"], failures(r)
    g = gate(r, "aptitude")
    print(f"aptitude: {g['note']}; warnings: {g['warnings']}; gzip: {gate(r, 'gzip')['note']}")


def test_real_aptitude_deck_sample():
    """Optional real chart-stats output, wrapped without modifying its statistics or touching the source file."""
    stats = json.loads(real("MUSIC_DATA_APTITUDE_DECK_SAMPLE"))
    assert stats["format"] == "ournotes-deck.chart-stats/3"
    doc = {"deck": {k: stats[k] for k in ("model", "kinds", "gekisouAptitude")},
           "songs": [{"id": c["musicId"], "charts": [{"scoreId": c["scoreId"], "difficulty": c["difficulty"],
                       "skillEventsMs": [e[1] for e in c["events"]], "notes": {"judged": c["judgedNotes"]},
                       "deck": c}]} for c in stats["charts"]]}
    report = gates(json.dumps(doc).encode(), Context(), only=("deck", "aptitude", "gzip"))
    assert report["passed"], failures(report)


# ---------------------------------------------------------------- publish (a stand-in bucket)
class FakeS3:
    def __init__(self, corrupt=None):
        self.store, self.log, self.corrupt, self.headers = {}, [], corrupt, {}

    def upload_file(self, src, bucket, key, ExtraArgs):
        self.store[key] = b"not it" if key == self.corrupt else Path(src).read_bytes()
        self.headers[key] = ExtraArgs
        self.log.append((key, ExtraArgs["CacheControl"], ExtraArgs["ContentType"]))

    def upload_fileobj(self, stream, bucket, key, ExtraArgs):
        self.store[key] = b"not it" if key == self.corrupt else stream.read()
        self.headers[key] = ExtraArgs
        self.log.append((key, ExtraArgs["CacheControl"], ExtraArgs["ContentType"]))

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.store[Key]), **self.headers.get(Key, {})}

    def head_object(self, Bucket, Key):
        from botocore.exceptions import ClientError
        if Key not in self.store:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        return {"ContentLength": len(self.store[Key])}


class FakeBucket:
    name, prefix, writable = "moenotes", "music-data/", True

    def __init__(self, s3):
        self.s3 = s3

    def object_size(self, key):
        raw = self.s3.store.get(self.prefix + key)
        return len(raw) if raw is not None else None


def published_out(tmp_path, monkeypatch, s3):
    out = tmp_path / "out"
    out.mkdir()
    raw, ctx = context(out, sample())
    report = gates(raw, ctx)
    (out / "check.json").write_text(json.dumps(report), encoding="utf-8")
    source = music_data.snapshot_identity("hk-tw-mo", ctx.snapshot["entry"], ctx.snapshot["files"])
    (out / "build.json").write_text(json.dumps({"sha256": report["sha256"], "sourceSnapshot": source,
                                                "archive": f"archive/v-test/{report['sha256']}.json"}),
                                    encoding="utf-8")
    monkeypatch.setattr(music_data, "bucket", lambda: FakeBucket(s3))
    monkeypatch.setattr(music_data.http_compression, "get_object",
                        lambda url, **kw: s3.get_object(Bucket="moenotes", Key="music-data/" + url.split("/music-data/", 1)[1]))
    monkeypatch.setattr(music_data.time, "sleep", lambda s: None)
    monkeypatch.setenv("STORY_S3_ENDPOINT", "https://storage.example")
    monkeypatch.setenv("STORY_S3_BUCKET", "moenotes")
    monkeypatch.setenv("MASTERDATA_REGION", "hk-tw-mo")
    monkeypatch.setattr(music_data.story_site, "master_index", lambda: ("unused", ctx.snapshot))
    monkeypatch.delenv("FORCE", raising=False)
    monkeypatch.setenv("MUSIC_DATA_PUBLISH", "true")
    return out, report


def test_publish_order_and_read_back(tmp_path, monkeypatch):
    s3 = FakeS3()
    out, report = published_out(tmp_path, monkeypatch, s3)
    music_data.cmd_publish(str(out))
    keys = [k for k, _, _ in s3.log]
    archive = f"music-data/archive/v-test/{report['sha256']}.json"
    assert sorted(keys[:2]) == ["music-data/jackets/jkt_100001.webp", "music-data/jackets/jkt_100002.webp"]
    assert keys[2:] == [archive, "music-data/music-data.json", "music-data/music-data.json.br", "music-data/build.json"]
    caches = {k: c for k, c, _ in s3.log}
    assert caches[archive].endswith("immutable") and caches["music-data/music-data.json"] == "no-cache"
    assert caches["music-data/music-data.json.br"] == "no-cache"
    assert music_data.http_compression.decode_content(s3.store["music-data/music-data.json"], "gzip") == (out / "music-data.json").read_bytes()
    assert s3.headers["music-data/music-data.json"]["ContentEncoding"] == "gzip"
    assert music_data.http_compression.decode_content(s3.store["music-data/music-data.json.br"], "br") == (out / "music-data.json").read_bytes()
    assert s3.headers["music-data/music-data.json.br"]["ContentEncoding"] == "br"
    s3.log.clear()
    music_data.cmd_publish(str(out))                          # again: the jackets and the archive copy are there
    assert [k for k, _, _ in s3.log] == ["music-data/music-data.json", "music-data/music-data.json.br", "music-data/build.json"]
    monkeypatch.setenv("FORCE", "true")
    s3.log.clear()
    music_data.cmd_publish(str(out), dry_run=True)
    assert s3.log == []


def test_publish_stops_before_the_marker_when_the_file_does_not_read_back(tmp_path, monkeypatch):
    s3 = FakeS3(corrupt="music-data/music-data.json")
    out, _ = published_out(tmp_path, monkeypatch, s3)

    def offline(*a, **kw):
        raise music_data.urllib.error.URLError("offline")
    monkeypatch.setattr(music_data, "get", offline)
    monkeypatch.setattr(music_data.http_compression, "get_object", offline)
    with pytest.raises(SystemExit, match="music-data.json: the bucket does not serve what was uploaded"):
        music_data.cmd_publish(str(out))
    assert "music-data/build.json" not in s3.store


@pytest.mark.parametrize("switch", [None, "", "false", "True", "1"])
def test_publishing_is_off_unless_the_switch_is_true(tmp_path, monkeypatch, switch):
    s3 = FakeS3()
    out, _ = published_out(tmp_path, monkeypatch, s3)
    if switch is None:
        monkeypatch.delenv("MUSIC_DATA_PUBLISH")
    else:
        monkeypatch.setenv("MUSIC_DATA_PUBLISH", switch)
    music_data.cmd_publish(str(out))                          # a dry run, whatever the command line says
    assert s3.log == [] and s3.store == {}


def test_publish_needs_passed_gates(tmp_path, monkeypatch):
    s3 = FakeS3()
    out, report = published_out(tmp_path, monkeypatch, s3)
    (out / "check.json").write_text(json.dumps(dict(report, passed=False)), encoding="utf-8")
    with pytest.raises(SystemExit, match="has not passed the gates"):
        music_data.cmd_publish(str(out))
    (out / "check.json").write_text(json.dumps(report), encoding="utf-8")
    (out / "music-data.json").write_bytes(b"{}")                         # not the file that was checked
    with pytest.raises(SystemExit, match="has not passed the gates"):
        music_data.cmd_publish(str(out))
    assert s3.store == {}


def replay_path(out, doc, name):
    return (out / doc["replay"]["manifestUrl"]).parent / name


def replay_key(doc, name):
    return "music-data/" + (Path(doc["replay"]["manifestUrl"]).parent / name).as_posix()


def rewrite_replay_manifest(out, doc, manifest):
    """The authentic producer puts every bundle under its manifest's decoded SHA."""
    raw_manifest = json.dumps(manifest).encode()
    previous = replay_path(out, doc, "manifest.json").parent
    sha = music_data.sha256(raw_manifest)
    current = out / "replay" / sha
    if previous != current:
        previous.rename(current)
    (current / "manifest.json").write_bytes(raw_manifest)
    doc["replay"].update(manifestUrl=f"replay/{sha}/manifest.json", sha256=sha)
    raw = json.dumps(doc).encode()
    (out / music_data.FILE).write_bytes(raw)
    for file in ("check.json", "build.json"):
        info = json.loads((out / file).read_bytes())
        info.update(sha256=music_data.sha256(raw), bytes=len(raw))
        if file == "build.json":
            info["replay"] = doc["replay"]
        (out / file).write_text(json.dumps(info))


def replay_out(out, recommend=True):
    """Small ABI resources: exercise publication identity/order without game assets."""
    doc = json.loads((out / music_data.FILE).read_bytes())
    commit = doc["provenance"]["deck"]["commit"]
    base = out / "replay" / "staging"
    def resource(url, raw):
        path = base / url
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {"url": url, "sha256": music_data.sha256(raw), "bytes": len(raw)}
    def engine(prefix, module, js_raw, wasm_raw):
        # the files nnnotes takes from an ournotes-deck release package: web JS, web WASM and build-info.json
        stem = f"ournotes_{module}_wasm"
        js = resource(f"{prefix}/{stem}.js", js_raw)
        wasm = resource(f"{prefix}/{stem}_bg.wasm", wasm_raw)
        built = resource(f"{prefix}/build-info.json", json.dumps({
            "version": "0.0.2", "tag": "v0.0.2", "commit": commit, "kind": "wasm", "module": module,
            "files": {f"web/{stem}.js": js["sha256"], f"web/{stem}_bg.wasm": wasm["sha256"]}}).encode())
        return {"model": {"commit": commit}, "js": js, "wasm": wasm, "build": built}
    charts = [{"scoreId": c["scoreId"], **resource(f"charts/{c['scoreId']}.json", b"{}")}
              for song in doc["songs"] for c in song["charts"]]
    manifest = {"format": "nnnotes.replay-manifest/1", "deckData": resource("deck-data.json", b"{}"),
                "charts": charts, "engine": {"requestFormat": "ournotes.replay/1", **engine(
                    "engine", "replay", b"export class ReplaySession {}", b"\0asm\x01\0\0\0")}}
    if recommend:
        manifest["recommendEngine"] = engine("recommend", "recommend", b"export class RecommendationSession {}",
                                             b"\0asm\x01\0\0\0recommend")
    raw_manifest = json.dumps(manifest).encode()
    resource("manifest.json", raw_manifest)
    doc["replay"] = {"format": manifest["format"], "manifestUrl": "replay/staging/manifest.json",
                     "sha256": music_data.sha256(raw_manifest), "charts": len(charts)}
    rewrite_replay_manifest(out, doc, manifest)
    return doc, manifest


def test_replay_resources_upload_before_document_and_marker(tmp_path, monkeypatch):
    s3 = FakeS3()
    out, _ = published_out(tmp_path, monkeypatch, s3)
    doc, _ = replay_out(out)
    paths = music_data.replay_resources(out, doc)
    assert paths[-1].name == "manifest.json"
    music_data.cmd_publish(str(out))
    keys = [k for k, _, _ in s3.log]
    runtime = ["music-data/" + p.relative_to(out).as_posix() for p in paths]
    assert keys[-4:] == [runtime[-1], "music-data/music-data.json", "music-data/music-data.json.br", "music-data/build.json"]
    assert set(runtime[:-1]).issubset(keys[:-4])
    assert {replay_key(doc, f"recommend/{name}") for name in ("ournotes_recommend_wasm.js",
                                                              "ournotes_recommend_wasm_bg.wasm",
                                                              "build-info.json")}.issubset(keys[:-4])
    types = {k: mime for k, _, mime in s3.log}
    assert types[replay_key(doc, "engine/ournotes_replay_wasm_bg.wasm")] == "application/wasm"
    assert types[replay_key(doc, "recommend/ournotes_recommend_wasm_bg.wasm")] == "application/wasm"
    assert types[replay_key(doc, "recommend/ournotes_recommend_wasm.js")] == "text/javascript"
    caches = {k: c for k, c, _ in s3.log}
    for path in paths:
        key = "music-data/" + path.relative_to(out).as_posix()
        assert caches[key] == "public, max-age=31536000, immutable"
        assert s3.headers[key]["ContentEncoding"] == "gzip"
        assert music_data.http_compression.decode_content(s3.store[key], "gzip") == path.read_bytes()
    assert caches["music-data/music-data.json"] == caches["music-data/build.json"] == "no-cache"
    marker = json.loads(music_data.http_compression.decode_content(s3.store["music-data/build.json"], "gzip"))
    assert marker["replay"] == doc["replay"]


def test_publish_stops_when_the_marker_names_another_replay_manifest(tmp_path, monkeypatch):
    s3 = FakeS3()
    out, _ = published_out(tmp_path, monkeypatch, s3)
    replay_out(out)
    marker = json.loads((out / music_data.MARKER).read_bytes())
    marker["replay"] = dict(marker["replay"], manifestUrl="replay/" + "f" * 64 + "/manifest.json", sha256="f" * 64)
    (out / music_data.MARKER).write_text(json.dumps(marker))
    with pytest.raises(SystemExit, match="replay pointer is not the file's"):
        music_data.cmd_publish(str(out))
    assert not s3.log


@pytest.mark.parametrize("recommend", [True, False])
def test_check_writes_the_replay_pointer_into_the_marker(tmp_path, monkeypatch, recommend):
    s3 = FakeS3()
    out, _ = published_out(tmp_path, monkeypatch, s3)
    doc, _ = replay_out(out, recommend=recommend)
    (out / music_data.MARKER).unlink()
    raw = (out / music_data.FILE).read_bytes()
    master = tmp_path / "master"
    master.mkdir(exist_ok=True)
    snapshot = {"region": "hk-tw-mo", "entry": {"version": "v-test"}, "files": {music_data.MANIFEST: "ab" * 32}}
    music_data.snapshot_file(master).write_text(json.dumps(snapshot), encoding="utf-8")
    passed = {"passed": True, "sha256": music_data.sha256(raw), "bytes": len(raw), "gates": []}
    monkeypatch.setattr(music_data, "gates", lambda raw, ctx: copy.deepcopy(passed))
    monkeypatch.setattr(music_data, "published", lambda key: None)
    monkeypatch.setattr(music_data, "deck_commit", lambda: DECK)
    monkeypatch.setattr(music_data, "inputs", lambda entry, files=None: {"recipe": music_data.RECIPE})
    monkeypatch.setenv("NNNOTES_CATALOG_REGION", "tw")
    monkeypatch.setitem(sys.modules, "nnnotes", type(sys)("nnnotes"))
    sys.modules["nnnotes"].__version__ = "0.1.2"
    if not recommend:
        with pytest.raises(SystemExit, match="a gate failed"):
            music_data.cmd_check(str(out), str(master), str(tmp_path / "page"))
        check = json.loads((out / "check.json").read_bytes())
        assert gate(check, "replay")["failures"] == ["final replay manifest has no recommend engine"]
        assert not (out / music_data.MARKER).exists()
        return
    music_data.cmd_check(str(out), str(master), str(tmp_path / "page"))
    marker = json.loads((out / music_data.MARKER).read_bytes())
    assert marker["replay"] == doc["replay"]
    assert marker["replay"]["manifestUrl"] == f"replay/{marker['replay']['sha256']}/manifest.json"
    assert (out / marker["replay"]["manifestUrl"]).is_file()


@pytest.mark.parametrize("manifest_url", ["replay/manifest.json", "replay/" + "f" * 64 + "/manifest.json"])
def test_mutable_or_wrong_digest_runtime_pointer_is_rejected_before_any_upload(tmp_path, monkeypatch, manifest_url):
    s3 = FakeS3()
    out, _ = published_out(tmp_path, monkeypatch, s3)
    doc, _ = replay_out(out)
    doc["replay"]["manifestUrl"] = manifest_url
    raw = json.dumps(doc).encode(); (out / music_data.FILE).write_bytes(raw)
    for name in ("check.json", "build.json"):
        report = json.loads((out / name).read_bytes()); report["sha256"] = music_data.sha256(raw)
        (out / name).write_text(json.dumps(report))
    with pytest.raises(SystemExit, match="immutable SHA directory"):
        music_data.cmd_publish(str(out))
    assert not s3.log


def test_runtime_payload_cannot_escape_to_a_shared_mutable_key(tmp_path, monkeypatch):
    s3 = FakeS3()
    out, _ = published_out(tmp_path, monkeypatch, s3)
    doc, manifest = replay_out(out)
    # The declared bytes/hash are valid, but this key would be shared across bundles.
    (out / "replay/shared.json").write_bytes(b"{}")
    manifest["deckData"]["url"] = "../shared.json"
    rewrite_replay_manifest(out, doc, manifest)
    with pytest.raises(SystemExit, match="immutable bundle directory"):
        music_data.cmd_publish(str(out))
    assert not s3.log


def test_both_shared_songs_publishers_hold_one_global_lock_then_prebuilt_holds_region_lock():
    workflows = Path(__file__).resolve().parent.parent / "workflows"
    prebuilt = (workflows / "music-data-prebuilt.yml").read_text()
    page = (workflows / "songs-page.yml").read_text()
    for workflow in (prebuilt, page):
        assert "\nconcurrency:\n" in workflow
        assert "\n  group: songs-page-publication\n  cancel-in-progress: false" in workflow
    assert "\n    concurrency:\n      group: music-data-${{ inputs.region }}\n      cancel-in-progress: false" in prebuilt


@pytest.mark.parametrize("defect", [None, "bytes", "region", "table_hash"])
def test_replay_labels_are_checked_and_uploaded_before_manifest(tmp_path, monkeypatch, defect):
    s3 = FakeS3()
    out, _ = published_out(tmp_path, monkeypatch, s3)
    doc, manifest = replay_out(out)
    tables = {name: {"sha256": "ab" * 32, "rows": []} for name in music_data.REPLAY_LABEL_TABLES}
    for name in tables:
        doc["provenance"]["master"]["tables"][name] = {"sha256": "ab" * 32}
    labels = {"format": "nnnotes.replay-labels/1", "region": doc["provenance"]["region"],
              "masterVersion": doc["provenance"]["master"]["version"], "tables": tables}
    if defect == "region":
        labels["region"] = "jp"
    elif defect == "table_hash":
        labels["tables"]["MasterText"]["sha256"] = "cd" * 32
    raw = json.dumps(labels).encode()
    path = replay_path(out, doc, "snap-labels.json")
    path.write_bytes(raw)
    manifest["snapLabels"] = {"format": labels["format"], "url": "snap-labels.json", "sha256": music_data.sha256(raw), "bytes": len(raw)}
    rewrite_replay_manifest(out, doc, manifest)
    path = replay_path(out, doc, "snap-labels.json")
    if defect == "bytes":
        path.write_bytes(b"tampered labels")
    if defect:
        with pytest.raises(SystemExit, match="replay resources changed after gates"):
            music_data.cmd_publish(str(out))
        assert not s3.store
    else:
        music_data.cmd_publish(str(out))
        keys = [key for key, _, _ in s3.log]
        assert keys.index(replay_key(doc, "snap-labels.json")) < keys.index(replay_key(doc, "manifest.json"))


@pytest.mark.parametrize("changed", ["bytes", "model", "escape", "absolute", "ids", "abi", "module", "recommend-bytes",
                                     "recommend-model", "recommend-commit", "recommend-files"])
def test_replay_identity_failure_prevents_any_upload(tmp_path, monkeypatch, changed):
    s3 = FakeS3()
    out, _ = published_out(tmp_path, monkeypatch, s3)
    doc, manifest = replay_out(out)
    if changed == "bytes":
        replay_path(out, doc, "deck-data.json").write_bytes(b"tampered")
    else:
        if changed == "model":
            manifest["engine"]["model"]["commit"] = "other"
        elif changed == "escape":
            manifest["deckData"]["url"] = "../../escaped.json"
        elif changed == "absolute":
            manifest["deckData"]["url"] = str(replay_path(out, doc, "deck-data.json").resolve())
        elif changed == "ids":
            manifest["charts"][0]["scoreId"] = -1
        elif changed == "abi":
            manifest["engine"]["requestFormat"] = "other"
        elif changed == "recommend-bytes":
            replay_path(out, doc, "recommend/ournotes_recommend_wasm_bg.wasm").write_bytes(b"\0asm\x01\0\0\0other")
        elif changed == "recommend-model":
            manifest["recommendEngine"]["model"]["commit"] = "other"
        elif changed in ("module", "recommend-commit", "recommend-files"):
            # a build-info.json that names another module, commit or file than the manifest's engine
            key, prefix = ("engine", "engine") if changed == "module" else ("recommendEngine", "recommend")
            built = replay_path(out, doc, f"{prefix}/build-info.json")
            data = json.loads(built.read_bytes())
            if changed == "module":
                data["module"] = "recommend"
            elif changed == "recommend-commit":
                data["commit"] = "b" * 40
            else:
                data["files"]["web/ournotes_recommend_wasm_bg.wasm"] = "0" * 64
            raw = json.dumps(data).encode()
            built.write_bytes(raw)
            manifest[key]["build"].update(sha256=music_data.sha256(raw), bytes=len(raw))
        rewrite_replay_manifest(out, doc, manifest)
    with pytest.raises(SystemExit, match="replay resources changed after gates"):
        music_data.cmd_publish(str(out))
    assert not s3.store


def test_replay_read_back_failure_stops_before_runtime_pointer(tmp_path, monkeypatch):
    s3 = FakeS3()
    out, _ = published_out(tmp_path, monkeypatch, s3)
    doc, _ = replay_out(out)
    s3.corrupt = replay_key(doc, "engine/ournotes_replay_wasm_bg.wasm")
    monkeypatch.setattr(music_data, "get", lambda *a, **kw: (_ for _ in ()).throw(music_data.urllib.error.URLError("offline")))
    monkeypatch.setattr(music_data.http_compression, "get_object", lambda *a, **kw: (_ for _ in ()).throw(music_data.urllib.error.URLError("offline")))
    with pytest.raises(SystemExit, match="does not serve what was uploaded"):
        music_data.cmd_publish(str(out))
    assert replay_key(doc, "manifest.json") not in s3.store
    assert "music-data/music-data.json" not in s3.store
    assert "music-data/music-data.json.br" not in s3.store
    assert "music-data/build.json" not in s3.store


@pytest.mark.parametrize("corrupt", [None, "archive", "runtime"])
def test_parallel_payload_readbacks_finish_before_any_pointer(tmp_path, monkeypatch, corrupt):
    s3 = FakeS3()
    out, _ = published_out(tmp_path, monkeypatch, s3)
    doc, _ = replay_out(out)
    paths = music_data.replay_resources(out, doc)
    archive = json.loads((out / music_data.MARKER).read_bytes())["archive"]
    payloads = {archive, *(p.relative_to(out).as_posix() for p in paths[:-1])}
    pointers = [paths[-1].relative_to(out).as_posix(), music_data.FILE, music_data.FILE_BR, music_data.MARKER]
    if corrupt:
        key = archive if corrupt == "archive" else replay_key(doc, "engine/ournotes_replay_wasm_bg.wasm").removeprefix("music-data/")
        s3.corrupt = "music-data/" + key
        monkeypatch.setattr(music_data, "get", lambda *a, **kw: (_ for _ in ()).throw(music_data.urllib.error.URLError("offline")))
        monkeypatch.setattr(music_data.http_compression, "get_object", lambda *a, **kw: (_ for _ in ()).throw(music_data.urllib.error.URLError("offline")))
    original = music_data.read_back
    pair, lock, verified, started = threading.Barrier(2), threading.Lock(), set(), []
    def read_back(b, key, digest):
        if key in payloads:
            with lock:
                started.append(key)
                overlap = len(started) <= 2
            if overlap:
                pair.wait(timeout=5)  # Sequential payload execution cannot pass.
        else:
            assert payloads <= verified  # Upload completion alone is insufficient.
        original(b, key, digest)
        with lock:
            verified.add(key)
    monkeypatch.setattr(music_data, "read_back", read_back)
    if corrupt:
        with pytest.raises(SystemExit, match="does not serve what was uploaded"):
            music_data.cmd_publish(str(out))
        assert all("music-data/" + key not in s3.store for key in pointers)
    else:
        music_data.cmd_publish(str(out))
        assert payloads <= verified
        assert [key for key, _, _ in s3.log][-len(pointers):] == ["music-data/" + key for key in pointers]


@pytest.mark.parametrize("change", [None, "sha", "missing"])
def test_engines_are_the_release_packages_of_the_pinned_model(tmp_path, monkeypatch, change):
    monkeypatch.setattr(music_data, "deck_package", lambda: ("0.0.2", "a" * 40))
    packages = {f"ournotes-{module}-wasm-v0.0.2.tar.gz": f"package {module}".encode() for module in music_data.ENGINES}
    sums = {name: music_data.sha256(raw) for name, raw in packages.items()}
    if change == "sha":
        sums["ournotes-recommend-wasm-v0.0.2.tar.gz"] = "0" * 64
    elif change == "missing":
        del sums["ournotes-recommend-wasm-v0.0.2.tar.gz"]
    base = f"{music_data.DECK_RELEASES}/v0.0.2/"
    def get(url, timeout=120):
        assert url.startswith(base)
        name = url.removeprefix(base)
        if name == "SHA256SUMS":
            return "".join(f"{digest}  {file}\n" for file, digest in sums.items()).encode()
        return packages[name]
    monkeypatch.setattr(music_data, "get", get)
    if change:
        with pytest.raises(SystemExit, match="SHA-256 differs" if change == "sha" else "publishes no"):
            music_data.release_engines(tmp_path)
        return
    replay, recommend = music_data.release_engines(tmp_path)
    assert (replay.name, recommend.name) == ("ournotes-replay-wasm-v0.0.2.tar.gz", "ournotes-recommend-wasm-v0.0.2.tar.gz")
    assert replay.read_bytes() == packages[replay.name] and recommend.read_bytes() == packages[recommend.name]
