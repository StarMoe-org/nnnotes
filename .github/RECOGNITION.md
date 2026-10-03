# Recognition gallery workflow (StarMoe)

`.github/workflows/recognition.yml` keeps the screenshot recognition of MoeNotes' card box current. The browser
recognizes Member and Snap cards in a player's screenshots with a Worker that matches SIFT features (OpenCV WASM) against
a gallery of every card and reads the visible level fields with an ONNX model (ONNX Runtime Web). Everything that Worker
loads lives in the story site's bucket (`https://storage.bdon.moe/moenotes/`), so a new card needs no MoeNotes release:
the next run of this workflow adds it.

| Object | Content | Cache-Control |
|---|---|---|
| `assets/<sha256>.<ext>` | every file of a bundle under the SHA-256 of its bytes: card artwork (`.webp`), the gallery buffers (`.bin`), OpenCV and ONNX Runtime (`.js`, `.mjs`, `.wasm`), the field model (`.onnx`), the gallery, field and bundle manifests (`.json`) | `public, max-age=31536000, immutable` |
| `recognition/current.json` | the pointer: `{"format": "moenotes.recognition-pointer/1", "bundle": {"sha256", "bytes"}}`, the current bundle manifest | `no-cache` |

Objects are stored as their bytes, without `Content-Encoding`, with the Content-Type of their extension (`.wasm`
`application/wasm`, `.json` `application/json`, `.js`/`.mjs` `text/javascript; charset=utf-8`, `.webp` `image/webp`,
`.onnx`/`.bin` `application/octet-stream`). The bucket answers GET requests with `Access-Control-Allow-Origin: *`, which
the site's Worker needs to load them from another origin. A run never deletes or overwrites an object: every bundle ever
published stays complete, and the pointer is the only object that changes.

A consumer reads the pointer (no cache), fetches `assets/<sha256>.json` and checks its SHA-256 and size, then fetches each
file the bundle lists and checks it the same way.

## Manifests

- **Bundle** (`moenotes.recognition-bundle/1`): `entries` names the gallery (`gallery/manifest.json`) and the field
  reader (`fields/manifest.json`); `files` maps every logical file of the bundle (`gallery/…`, `opencv/…`, `fields/…`,
  `art/member/<id>.webp`, `art/snap/<id>.webp`) to its `path`, `sha256`, `bytes` and `contentType`; `previous` is the
  bundle the pointer named when this one was built (null for the first); `inputsSha256` identifies what it was made from;
  `counts` the cards per kind.
- **Gallery** (`ournotes.browser-feature-gallery/3`): `cards` (Member cards, then Snap cards, each by id) with their
  card size, `identity` (asset id, character ids, rarity, card type: what a server's catalogue must match for the entry
  to apply), the `regions` whose master data has that identity, and `art` (the artwork file, its asset service path,
  bytes, SHA-256, size and how the card image is derived from it: `fit`, a crop box resampled with Lanczos-3 to the card
  size, or `direct`); `buffers` (`descriptors` float32 N×128 RootSIFT rows, `points` float32 N×2 in card pixels, `owners`
  int32 N, the index of each row's card); `opencv` (glue and WASM); `catalog` (each region's master version and card
  table hashes); `galleryId`, the SHA-256 of the manifest's canonical JSON without it (keys sorted, no spaces, numbers as
  JavaScript writes them). File names are relative to the manifest.
- **Field reader** (`ournotes.browser-cultivation-assets/2`): the ONNX Runtime `runtime` (`glue`, `module`, `wasm`) and the
  `model`, each with `file`, `size` and `sha256`.

## A run

1. **plan** (seconds): the cards of `MasterMemberCard` and `MasterSupportCard` of every region of `RECOGNITION_REGIONS`
   from moenotes-masterdata-sync (each table's SHA-256 checked against its `index.json`), their union, and each card's
   artwork entry in the asset service listing; then the published pointer, bundle and gallery over plain HTTP.
   `inputsSha256` (the recipe, its pinned libraries, the runtime pins, every card's identity, regions and artwork) equal
   to the published bundle's: the run ends here, unless `force`. A catalog without a card the published gallery has
   stops the run. A pinned runtime file the bucket lacks starts the runtime job.
2. **runtime** (only while a pinned file is missing): the missing files of `recognition-runtime.json` from this
   repository's release named by its `release` tag (a draft release; assets named `<sha256>.<ext>`), each checked
   against its pin, uploaded and read back over public HTTP.
3. **build**:
   - the pinned libraries (`requirements` of the gallery builder) and the self-test;
   - every card's artwork from the asset service (`/files/…` of its listing), SHA-256, size and decoded size checked; any
     failure stops the run before anything is uploaded;
   - the card images: a Member card's square artwork cropped to the list's 212×282 portrait (`ImageOps.fit`, centred) and
     resampled with Lanczos, a Snap card's artwork as it is;
   - the features, as ournotes-boxlens builds its index: OpenCV SIFT (260 features, contrast threshold 0.018, edge
     threshold 12) on the grayscale image scaled to at most 220 pixels wide, with its three corner masks, and RootSIFT
     normalization;
   - the gallery and field manifests, the bundle manifest (with `previous`) and the new pointer, laid out as in the
     bucket under `out/site`.
4. **publish**: a read-only preflight GETs every object of the bundle; an object already there with the same bytes,
   Content-Type and CORS header is kept, one that differs stops the run without any write. The published pointer must
   still name `previous` (or already this bundle), and the gallery must keep every published card. A dry run ends here
   and lists what it would upload. Then the missing objects are created (create-only, `If-None-Match: *`) in order:
   artwork and buffers, the gallery and field manifests, the bundle manifest; each is read back over public HTTP (bytes,
   SHA-256, Content-Type, `Access-Control-Allow-Origin`). After the whole bundle is read back once more, the pointer is
   checked again and written last, then read back. Pinned runtime files are only checked here: the runtime job uploads
   them.

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
list object names, paths, SHA-256, sizes, Content-Types and states only; artwork and feature buffers are never artifacts.

## Settings

Repository secrets: the story site's (`.github/STORY_SITE.md`), no new one: `STORY_S3_ACCESS_KEY`, `STORY_S3_SECRET_KEY`.

Repository variables (optional; the defaults are the StarMoe services): `RECOGNITION_PUBLISH` (unset: off),
`RECOGNITION_REGIONS` (`hk-tw-mo en kr jp`; the first region with a card gives its identity and artwork),
`RECOGNITION_ASSET_API` (`https://assets.bdon.moe`), `RECOGNITION_S3_PREFIX` (empty: the bucket root),
`MASTERDATA_BASE_URL` (`https://metadata.bdon.moe`), `STORY_S3_ENDPOINT` (`https://storage.bdon.moe`), `STORY_S3_BUCKET`
(`moenotes`).

The asset service publishes a region's files under `/{region}/{language}/`: `hk-tw-mo` `tw/zh-Hans`, `en` `en/en`, `kr`
`kr/ko`, `jp` `jp/ja`.

## Runtime and models

`recognition-runtime.json` pins the files that change only with a new model or runtime release: OpenCV (`opencv/`), ONNX
Runtime and the field model (`fields/`), each by SHA-256, size and extension, and the gallery builders with their
libraries. To ship a new runtime or model file:

1. attach it to a draft release of this repository as `<sha256>.<ext>`;
2. change its pin (and `release`, when the release is a new one) in a pull request;
3. the next run sees new inputs, uploads the file (runtime job) and publishes a bundle that references it.

`galleries` maps a gallery to its builder (`sift-rootsift/1`: the SIFT gallery above). A builder turns the card images
into buffers and the manifest's feature description; `recognition.py` keeps them in `BUILDERS` and refuses an unknown
one. A gallery of learned card embeddings (an encoder ONNX model run on the CPU over each card image, its vectors as a
buffer) is added as another builder with its encoder file pinned like the field model; the bundle then names it in
`entries` beside `gallery`.

## Notes

- **Card identity across regions.** A card id present in several regions normally has the same asset, characters,
  rarity and type everywhere; a region where it differs is left out of the card's `regions` and named in the job summary.
  MoeNotes uses a gallery entry for a server only when that server's catalogue has the same identity.
- **Artwork changes.** An artwork file that changes in the asset service changes `inputsSha256`: the next run publishes a
  bundle with the new file and its features; the report lists the card under `changedArtwork`.
- **Removed cards.** A catalog that lacks a published card is not published (the pointer stays). A person decides: if the
  game removed the card for good, change the gate in a pull request.
- **Bucket listings.** As in the other workflows, no step uses S3 listing; objects are checked by their own URLs.
- **Cache headers.** The storage gateway may answer with its own `Cache-Control`; the reports record what it returned.

The self-test runs in each build and locally in seconds, without the network (the feature test is skipped without
OpenCV):

```
python -m pytest -q -p no:cacheprovider .github/scripts/test_recognition.py
```
