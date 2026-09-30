// DRAFT: a small actual-WASM consumer smoke, in addition to the original page gate.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";

const [page, out] = process.argv.slice(2);
if (!page || !out) throw new Error("usage: prebuilt_publish_smoke.mjs <examples/songs> <out>");
for (const filename of ["replay-panel.js", "replay-worker.js", "replay-preset.js", "text.js", "catalog.js", "ranking.js"]) {
  await fs.access(path.join(page, filename));
  execFileSync(process.execPath, ["--check", path.join(page, filename)], { stdio: "pipe" });
}
const { parseJustJudgementTypes, applyAccuracyPreset, applySegmentPreset } = await import(pathToFileURL(path.join(page, "replay-preset.js")));
await import(pathToFileURL(path.join(page, "text.js")));
const main = JSON.parse(await fs.readFile(path.join(out, "music-data.json"), "utf8"));
const manifestPath = path.join(out, main.replay.manifestUrl);
const manifest = JSON.parse(await fs.readFile(manifestPath, "utf8"));
const base = path.dirname(manifestPath);
const dataJSON = await fs.readFile(path.join(base, manifest.deckData.url), "utf8");
const data = JSON.parse(dataJSON);
const types = parseJustJudgementTypes(data);
const engine = await import(pathToFileURL(path.join(base, manifest.engine.js.url)));
await engine.default({ module_or_path: await fs.readFile(path.join(base, manifest.engine.wasm.url)) });
const session = new engine.ReplaySession(dataJSON);
try {
  const first = data.charts[0].scoreId;
  const normal = JSON.parse(session.template(first, 300000, 60));
  const normalResult = JSON.parse(session.run(JSON.stringify(normal)));
  assert.equal(normalResult.complete, true);
  assert.ok(Number.isFinite(normalResult.score) && normalResult.score > 0);
  let description;
  for (const chart of data.charts) {
    const candidate = JSON.parse(session.describeChart(chart.scoreId));
    if (candidate.missions.includes(3)) { description = candidate; break; }
  }
  assert.ok(description, "final catalog must include a Just mission fixture");
  const cases = [];
  for (const kind of ["accuracy", "segments"]) {
    const request = JSON.parse(session.template(description.scoreId, 300000, 60));
    request.mode = { kind: "soloGekisou" };
    const counts = kind === "accuracy" ? applyAccuracyPreset(request, description, types, .3, .6)
      : applySegmentPreset(request, description, types,
        [{ startMs: null, endMs: null, great: .1, good: .05, bad: .05, miss: .05, just: 1 }], 42);
    assert.ok(counts.just > 0, "fixture must exercise an actual Just input");
    const result = JSON.parse(session.run(JSON.stringify(request)));
    assert.equal(result.complete, true);
    for (const key of ["perfect", "great", "just", ...(kind === "segments" ? ["good", "bad", "miss"] : [])]) {
      assert.equal(result.judgements[key], counts[key], `converted ${key} count`);
    }
    cases.push({ kind, scoreId: description.scoreId, score: result.score, counts });
  }
  console.log(JSON.stringify({ passed: true, scope: "final consumer modules + actual shared WASM; three replay runs", modelCommit: manifest.engine.model.commit, cases }));
} finally { session.free(); }
