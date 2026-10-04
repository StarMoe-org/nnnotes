# Recognition bundle workflow (StarMoe)

`.github/workflows/recognition.yml` keeps the screenshot recognition of MoeNotes' card box current. The browser
recognizes Member and Snap cards in a player's screenshots with a Worker that runs ONNX models on ONNX Runtime Web
(WASM): a locator finds every card tile and its kind, a per-kind encoder turns each tile's artwork window into an
embedding that is compared with a gallery of reference embeddings of every card, and per-kind readers read the visible
level or training count and the card rank. Everything that Worker loads lives in the story site's bucket
(`https://storage.bdon.moe/moenotes/`), so a new card needs no MoeNotes release: the next run of this workflow adds its
reference embedding.

| Object | Content | Cache-Control |
|---|---|---|
| `assets/<sha256>.<ext>` | every file of a bundle under the SHA-256 of its bytes: the embedding buffers (`.bin`), ONNX Runtime Web (`.js`, `.mjs`, `.wasm`), the models (`.onnx`), the gallery, model and bundle manifests (`.json`) | `public, max-age=31536000, immutable` |
| `recognition/current.json` | the pointer: `{"format": "moenotes.recognition-pointer/1", "bundle": {"sha256", "bytes"}}`, the current bundle manifest | `no-cache` |

Objects are stored as their bytes, without `Content-Encoding`, with the Content-Type of their extension (`.wasm`
`application/wasm`, `.json` `application/json`, `.js`/`.mjs` `text/javascript; charset=utf-8`, `.onnx`/`.bin`
`application/octet-stream`). The bucket answers GET requests with `Access-Control-Allow-Origin: *`, which the site's
Worker needs to load them from another origin. A run never deletes or overwrites an object: every bundle ever published
stays complete, and the pointer is the only object that changes.

A consumer reads the pointer (no cache), fetches `assets/<sha256>.json` and checks its SHA-256 and size, then fetches each
file the bundle lists and checks it the same way.

## Manifests

- **Bundle** (`moenotes.recognition-bundle/2`): `entries` names the gallery (`gallery/manifest.json`) and the models
  (`models/manifest.json`); `files` maps every logical file of the bundle (`gallery/…`, `models/…`, `runtime/…`) to its
  `path`, `sha256`, `bytes` and `contentType`; `previous` is the bundle the pointer named when this one was built (null
  for the first); `inputsSha256` identifies what it was made from; `counts` the cards per kind.
- **Gallery** (`moenotes.embedding-gallery/1`): `cards` (Member cards, then Snap cards, each by id) with `identity` (asset
  id, character ids, rarity, card type: what a server's catalogue must match for the entry to apply), the `regions` whose
  master data has that identity, `levelLimit` (the highest level the card reaches) and `art` (the artwork's asset service
  path, bytes, SHA-256 and size, and the `reference` resampling steps its encoder input was made with); `embeddings` per
  kind: the `encoder` model file that made them, the `dimension`, `cards` (the index in `cards` of each row) and `buffer`
  (float32 little-endian rows, `shape` [rows, dimension], each row L2-normalized); `catalog` (each region's master version
  and table hashes); `builder`; `galleryId`, the SHA-256 of the manifest's canonical JSON without it (keys sorted, no
  spaces, numbers as JavaScript writes them). File names are relative to the manifest.
- **Models** (`moenotes.recognition-models/1`): the ONNX Runtime Web `runtime` (`glue`, `module`, `wasm`), the card
  `tiles` (logical width and height per kind) and the stages `locator`, `encoders`, `fields` and `ranks` (per kind), each
  with its `model` (`file`, `sha256`, `bytes`) and the parameters of `pipeline` in `recognition-runtime.json`.

## The pipeline parameters

`recognition-runtime.json` `pipeline` holds every number the Worker applies to a model's input or output, so a
retrained model with new thresholds is a pin change only. Rectangles are `[x, y, w, h]` in screenshot pixels; a tile's
scale is `s = w / tile width`.

- `locator`: the screenshot is resized (area averaging) so its long edge is `longEdge`, placed at the top-left of a
  canvas whose sides are multiples of `multiple`, filled with grey `pad`; the input is RGB in 0..1. The outputs, on a
  grid of `stride` pixels: `heatmap` (one channel per entry of `classes`), `size` (tile width and height in input
  pixels) and `offset` (tile centre in cells from the cell origin). A tile is a 3×3 local maximum of at least
  `threshold`; tiles less than `minVisible` inside the screenshot are dropped, and by descending score a tile whose
  intersection with a kept tile exceeds `overlap` of the smaller one is dropped (at most `maxPeaks` peaks,
  `maxDetections` tiles).
- `encoders`: the artwork window, the tile less `inset` logical units on every side, sampled bilinearly (edge pixels
  repeated) to `input` (height, width); the embedding of an accepted card has a cosine similarity of at least
  `similarity` to its nearest reference of the same kind and exceeds the second nearest by at least `margin`. Reference
  inputs: a Member square fitted (centred, Lanczos) to the artwork window, then resized bilinearly; a Snap image cropped
  centred to the window's aspect and resized bilinearly.
- `fields`: the crop `left` logical units from the tile's left edge, `bottom` units above its bottom edge, `width` ×
  `height` units, sampled bilinearly (black outside the screenshot) to `input`; skipped (unknown) when the crop is less
  than `edge` pixels inside the screenshot. Of `classes` softmax outputs, 0 is another parameter or an unreadable field,
  1–100 a level, and for Member cards 101–105 a training count; a value is read when its probability is at least
  `confidence` and exceeds the second by at least `margin`, and a level above the card's `levelLimit` is not read.
- `ranks`: the `size` × `size` square around `center` (logical units), sampled like the fields to `input`; unknown when
  the `icon` box (width, height around `center`) is less than `edge` pixels inside the screenshot. Class 0 is no readable
  rank icon, 1–5 the card rank; acceptance as for the fields.

## A run

1. **plan** (seconds): the cards and level tables (`MasterMemberCard`, `MasterSupportCard`, `MasterMemberCardLevel`,
   `MasterMemberCardLevelLimit`, `MasterSupportCardLevel`, `MasterSupportCardRank`) of every region of
   `RECOGNITION_REGIONS` from moenotes-masterdata-sync (each table's SHA-256 checked against its `index.json`), their
   union, and each card's artwork entry in the asset service listing; then the published pointer, bundle and gallery over
   plain HTTP. `inputsSha256` (the recipe, every pin, the pipeline parameters, the gallery builder and its libraries,
   every card's identity, regions, level limit and artwork) equal to the published bundle's: the run ends here, unless
   `force`. A catalog without a card the published gallery has stops the run.
2. **runtime** (whenever a build follows, or a pinned file is missing): the missing files of
   `recognition-runtime.json` from this repository's release named by its `release` tag (a draft release; assets named
   `<sha256>.<ext>`), each checked against its pin, uploaded and read back over public HTTP; and the encoder models the
   gallery builder runs (from the bucket, or from the release while they are missing there), checked against their pins
   and handed to the build job as a one-day artifact.
3. **build**:
   - the pinned libraries (`requirements` of the gallery builder) and the self-test;
   - every card's artwork from the asset service (`/files/…` of its listing), SHA-256, size and decoded size checked; any
     failure stops the run before anything is uploaded;
   - each card's level limit: a Member card's level curve (`MasterMemberCardLevel` of its level group) capped by the
     highest level limit of its rarity (`MasterMemberCardLevelLimit`), a Snap card's curve capped by the highest level
     limit of its rank group (`MasterSupportCardRank`), from the card's first region;
   - the reference embeddings (`encoder-embed/1`): each card's reference input (above) run through the pinned encoder of
     its kind with ONNX Runtime on one CPU thread; every row must be a finite unit vector;
   - the gallery and models manifests, the bundle manifest (with `previous`) and the new pointer, laid out as in the
     bucket under `out/site`.
4. **publish**: a read-only preflight GETs every object of the bundle; an object already there with the same bytes,
   Content-Type and CORS header is kept, one that differs stops the run without any write. The published pointer must
   still name `previous` (or already this bundle), and the gallery must keep every published card. A dry run ends here
   and lists what it would upload. Then the missing objects are created (create-only, `If-None-Match: *`) in order:
   embedding buffers, the gallery and models manifests, the bundle manifest; each is read back over public HTTP (bytes,
   SHA-256, Content-Type, `Access-Control-Allow-Origin`). After the whole bundle is read back once more, the pointer is
   checked again and written last, then read back. Pinned files are only checked here: the runtime job uploads them.

A failed run leaves the pointer on the previous bundle; the objects it already created stay (they are content-addressed
and unreferenced), and the next run resumes: matching objects are kept, only missing ones are uploaded.

Triggers: `repository_dispatch` `masterdata-updated` (moenotes-masterdata-sync's `dispatch_repositories` already names
this repository: the story site and music data workflows run on it too), a daily schedule (03:53 UTC) in case a dispatch
was missed, and `workflow_dispatch`:

| Input | Meaning |
|---|---|
| `force` | build and publish although the published bundle was made from the same inputs |
| `dry_run` | build and check, then list what would be uploaded instead of uploading |

Runs do not overlap (`concurrency: recognition-gallery`).

**Publishing switch.** Nothing is uploaded unless the repository variable `RECOGNITION_PUBLISH` is `true` (unset: off).
Off, every run is a dry run, whatever its trigger: the runtime and publish steps get no bucket key and only read. Turn it
on with `gh variable set RECOGNITION_PUBLISH --body true`.

Reports (`report.json` of the build, `publication.json`, `runtime.json`, the new pointer) are kept as run artifacts. They
list object names, paths, SHA-256, sizes, Content-Types and states only; artwork and embedding buffers are never
artifacts.

## Settings

Repository secrets: the story site's (`.github/STORY_SITE.md`), no new one: `STORY_S3_ACCESS_KEY`, `STORY_S3_SECRET_KEY`.

Repository variables (optional; the defaults are the StarMoe services): `RECOGNITION_PUBLISH` (unset: off),
`RECOGNITION_REGIONS` (`hk-tw-mo en kr jp`; the first region with a card gives its identity, level limit and artwork),
`RECOGNITION_ASSET_API` (`https://assets.bdon.moe`), `RECOGNITION_S3_PREFIX` (empty: the bucket root),
`MASTERDATA_BASE_URL` (`https://metadata.bdon.moe`), `STORY_S3_ENDPOINT` (`https://storage.bdon.moe`), `STORY_S3_BUCKET`
(`moenotes`).

The asset service publishes a region's files under `/{region}/{language}/`: `hk-tw-mo` `tw/zh-Hans`, `en` `en/en`, `kr`
`kr/ko`, `jp` `jp/ja`.

## Runtime and models

`recognition-runtime.json` pins the files that change only with a new model or runtime release: ONNX Runtime Web
(`runtime/`) and the models (`models/`), each by SHA-256, size and extension; `runtime` names the glue, module and WASM
files; `pipeline` assigns every model to its stage and holds the stage parameters; `galleries` maps the gallery to its
builder with the builder's libraries. Every pinned file is used by exactly one runtime role or stage. To ship a new
runtime or model file:

1. attach it to a draft release of this repository as `<sha256>.<ext>`;
2. change its pin (and `release`, when the release is a new one) in a pull request;
3. the next run sees new inputs, uploads the file (runtime job) and publishes a bundle that references it.

A changed threshold or other pipeline parameter is a pull request to `pipeline` only; the next run publishes a bundle
with the new models manifest. A new encoder model changes every reference embedding: the next run computes them again.

`galleries.gallery.builder` names how the reference embeddings are made (`encoder-embed/1`, above); `recognition.py`
keeps the builders in `BUILDERS` and refuses an unknown one.

## Notes

- **Card identity across regions.** A card id present in several regions normally has the same asset, characters,
  rarity and type everywhere; a region where it differs is left out of the card's `regions` and named in the job summary.
  MoeNotes uses a gallery entry for a server only when that server's catalogue has the same identity.
- **Artwork changes.** An artwork file that changes in the asset service changes `inputsSha256`: the next run publishes a
  bundle with the new reference embedding; the report lists the card under `changedArtwork`.
- **Removed cards.** A catalog that lacks a published card is not published (the pointer stays). A person decides: if the
  game removed the card for good, change the gate in a pull request.
- **Bucket listings.** As in the other workflows, no step uses S3 listing; objects are checked by their own URLs.
- **Cache headers.** The storage gateway may answer with its own `Cache-Control`; the reports record what it returned.

The self-test runs in each build and locally in seconds, without the network (the ONNX Runtime test is skipped without
onnxruntime):

```
python -m pytest -q -p no:cacheprovider .github/scripts/test_recognition.py
```

A local build needs the encoder models (`--models`, files named after their pin or `<sha256>.<ext>`); `--runtime-dir`
adds every pinned file to the site for a local preview:

```
python .github/scripts/recognition.py build out --models DIR [--runtime-dir DIR]
```
