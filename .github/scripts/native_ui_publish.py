"""Publish the authorized compact TW UI closure to the existing moenotes bucket.

Transport bytes are frozen locally and checked again on the runner. Credentials
come from the existing STORY_S3 environment or an explicit local profile; reports
and exception output never include authentication material.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

MANIFEST_SHA = "0c7f77573ffb33957aa168cfd8816bf2d4a40ec67b87a26f188757a76e5132c2"
BUCKET = "moenotes"
ENDPOINT = "https://storage.bdon.moe"
ORIGIN = "http://127.0.0.1:4399"
CACHE = "public, max-age=31536000, immutable"
FORMAT = "moenotes.game-ui-publication/2"
METADATA = {"decoded-sha256": "decodedSha256", "decoded-bytes": "decodedBytes",
            "encoded-sha256": "encodedSha256", "encoded-bytes": "encodedBytes"}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def contained(root, name):
    require(isinstance(name, str) and "\\" not in name and ":" not in name
            and not name.startswith("/") and all(p not in ("", ".", "..") for p in name.split("/")),
            "invalid runtime path")
    candidate = root / name
    require(not candidate.is_symlink(), "symlink payload is forbidden")
    path = candidate.resolve()
    require(path.is_relative_to(root.resolve()), "runtime path escapes root")
    return path


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def no_local_paths(value):
    if isinstance(value, (dict, list)):
        for child in value.values() if isinstance(value, dict) else value:
            no_local_paths(child)
    elif isinstance(value, str):
        require(not (re.match(r"^[A-Za-z]:[/\\]", value)
                     or value.startswith(("/home/", "/Users/", "\\\\"))),
                "remaining local absolute path in publication manifest")


def manifest_records(raw):
    require(sha(raw) == MANIFEST_SHA, "manifest differs from authorized version")
    manifest = json.loads(raw)
    require(manifest.get("schema") == "moenotes.game-ui-library/1"
            and manifest.get("region") == "tw", "manifest schema or region differs")
    require(manifest.get("text", {}).get("mode") == "website"
            and manifest["text"].get("gameFontFilesIncluded") is False, "website font scope differs")
    require(manifest.get("motion", {}).get("controllersIncluded") is False, "animation scope differs")
    require(len(manifest["files"]) == 16, "runtime file count differs")
    no_local_paths(manifest)
    records = []
    for name, record in sorted(manifest["files"].items()):
        require(record["mime"] in ("application/json", "image/png"), "unexpected runtime MIME")
        records.append({"path": name, "decodedSha256": record["sha256"],
                        "decodedBytes": record["size"], "contentType": record["mime"]})
    records.append({"path": "manifest.json", "decodedSha256": MANIFEST_SHA,
                    "decodedBytes": len(raw), "contentType": "application/json"})
    return records


def inventory_for(items):
    prefix = f"game-ui/tw/{MANIFEST_SHA}/"
    return {"format": FORMAT, "bucket": BUCKET, "endpoint": ENDPOINT, "prefix": prefix,
            "manifestUrl": f"{ENDPOINT}/{BUCKET}/{prefix}manifest.json", "manifestSha256": MANIFEST_SHA,
            "objectCount": len(items), "decodedBytes": sum(i["decodedBytes"] for i in items),
            "encodedBytes": sum(i["encodedBytes"] for i in items), "cacheControl": CACHE,
            "corsOrigin": ORIGIN, "objects": items}


def require_file_set(root, records):
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    require(actual == {r["path"] for r in records}, "payload file inventory differs")


def prepare(args):
    source, work = args.source.resolve(), args.work.resolve()
    require(source != work and not work.is_relative_to(source), "work must be outside source closure")
    records = manifest_records(contained(source, "manifest.json").read_bytes())
    require_file_set(source, records)
    payload = work / "encoded-payload"
    require(not payload.exists(), "prepare work payload already exists")
    payload.mkdir(parents=True)
    items = []
    for record in records:
        decoded = contained(source, record["path"]).read_bytes()
        require(sha(decoded) == record["decodedSha256"] and len(decoded) == record["decodedBytes"],
                f"source identity differs: {record['path']}")
        encoding = "gzip" if record["contentType"] == "application/json" else None
        encoded = gzip.compress(decoded, compresslevel=6, mtime=0) if encoding else decoded
        require(not encoding or gzip.decompress(encoded) == decoded, "gzip roundtrip differs")
        target = contained(payload, record["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
        items.append({**record, "encodedSha256": sha(encoded), "encodedBytes": len(encoded),
                      "contentEncoding": encoding})
    inventory = inventory_for(items)
    write_json(work / "publication-inventory.json", inventory)
    if args.archive:
        require(not args.archive.exists(), "archive already exists")
        args.archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.archive, "x", compression=zipfile.ZIP_STORED) as archive:
            members = [("publication-inventory.json", work / "publication-inventory.json")]
            members.extend(("encoded-payload/" + i["path"], contained(payload, i["path"])) for i in items)
            for name, path in members:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type, info.create_system = zipfile.ZIP_STORED, 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
        checksum = sha(args.archive.read_bytes())
        args.archive.with_name(args.archive.name + ".sha256").write_text(
            f"{checksum}  {args.archive.name}\n", encoding="utf-8")
        write_json(work / "bundle-identity.json", {"archive": args.archive.name, "sha256": checksum,
                   "bytes": args.archive.stat().st_size, "manifestSha256": MANIFEST_SHA})
    print(json.dumps({k: inventory[k] for k in ("manifestUrl", "objectCount", "decodedBytes", "encodedBytes")}), flush=True)


def checked_inventory(args):
    inventory = json.loads(args.inventory.read_bytes())
    require(inventory.get("format") == FORMAT, "publication inventory format differs")
    payload = args.payload.resolve()
    raw = gzip.decompress(contained(payload, "manifest.json").read_bytes())
    records = manifest_records(raw)
    require(len(inventory["objects"]) == len(records), "publication object count differs")
    require_file_set(payload, records)
    for item, record in zip(inventory["objects"], records):
        require(all(item.get(k) == v for k, v in record.items()), "inventory decoded identity differs")
        encoding = "gzip" if record["contentType"] == "application/json" else None
        require(item.get("contentEncoding") == encoding, "inventory encoding differs")
        encoded = contained(payload, item["path"]).read_bytes()
        require(len(encoded) == item["encodedBytes"] and sha(encoded) == item["encodedSha256"],
                f"encoded payload identity differs: {item['path']}")
        decoded = gzip.decompress(encoded) if encoding else encoded
        require(len(decoded) == item["decodedBytes"] and sha(decoded) == item["decodedSha256"],
                f"decoded payload identity differs: {item['path']}")
    require(inventory == inventory_for(inventory["objects"]), "publication destination or totals differ")
    return inventory


def public_read(item, inventory, allow_missing=False):
    url = inventory["manifestUrl"].removesuffix("manifest.json") + item["path"]
    for attempt in range(4):
        request = urllib.request.Request(url, headers={"User-Agent": "MoeNotes-native-ui-publisher/2",
            "Accept-Encoding": "gzip", "Origin": ORIGIN})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                encoded, headers = response.read(), response.headers
                require(response.status == 200, "public response status differs")
            encoding = headers.get("Content-Encoding")
            require(encoding == item["contentEncoding"], f"public encoding differs: {item['path']}")
            require(len(encoded) == item["encodedBytes"] and sha(encoded) == item["encodedSha256"],
                    f"public encoded identity differs: {item['path']}")
            decoded = gzip.decompress(encoded) if encoding == "gzip" else encoded
            require(len(decoded) == item["decodedBytes"] and sha(decoded) == item["decodedSha256"],
                    f"public decoded identity differs: {item['path']}")
            require(headers.get("Content-Type", "").split(";", 1)[0] == item["contentType"],
                    f"public MIME differs: {item['path']}")
            observed_cache = headers.get("Cache-Control")
            require(observed_cache in (CACHE, "max-age=0"), f"unexpected public cache: {item['path']}")
            require(headers.get("Access-Control-Allow-Origin") in ("*", ORIGIN), f"public CORS missing: {item['path']}")
            for name, field in METADATA.items():
                require(headers.get("x-amz-meta-" + name) == str(item[field]),
                        f"public identity metadata differs: {item['path']}")
            return {**item, "httpStatus": 200, "corsAllowOrigin": headers.get("Access-Control-Allow-Origin"),
                    "cacheControlRequested": CACHE, "cacheControlObserved": observed_cache,
                    "cachePolicyHonored": observed_cache == CACHE, "verified": True}
        except urllib.error.HTTPError as error:
            if error.code == 404 and allow_missing:
                return None
            if error.code not in (404, 429, 500, 502, 503, 504) or attempt == 3:
                raise ValueError(f"public HTTP {error.code}: {item['path']}") from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise ValueError(f"public transport failed: {item['path']}") from None
        time.sleep(1 + attempt)


def s3_client(profile):
    import boto3
    from botocore.config import Config

    config = Config(s3={"addressing_style": "path"}, request_checksum_calculation="when_required",
        response_checksum_validation="when_required", retries={"max_attempts": 3, "mode": "adaptive"},
        max_pool_connections=8, connect_timeout=15, read_timeout=60)
    if profile:
        require(profile != "moly-sekai-writer", "known denied profile must not be retried")
        session = boto3.Session(profile_name=profile)
    else:
        key, secret = os.environ.get("STORY_S3_ACCESS_KEY", ""), os.environ.get("STORY_S3_SECRET_KEY", "")
        require(bool(key and secret), "existing project STORY_S3 publishing identity is required")
        session = boto3.Session(aws_access_key_id=key, aws_secret_access_key=secret)
    return session.client("s3", endpoint_url=ENDPOINT, region_name="us-east-1", config=config)


def cache_audit(args):
    inventory = checked_inventory(args)
    s3 = s3_client(args.profile)
    results = []
    for item in inventory["objects"]:
        url = inventory["manifestUrl"].removesuffix("manifest.json") + item["path"]
        try:
            request = urllib.request.Request(url, method="HEAD", headers={"Origin": ORIGIN})
            with urllib.request.urlopen(request, timeout=30) as response:
                public_cache = response.headers.get("Cache-Control")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            raise ValueError(f"public cache audit HTTP {error.code}: {item['path']}") from None
        source = s3.head_object(Bucket=BUCKET, Key=inventory["prefix"] + item["path"])
        raw_headers = source.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        raw_headers = {k.lower(): v for k, v in raw_headers.items()}
        results.append({"path": item["path"], "cacheControlRequested": CACHE,
            "publicCacheControlObserved": public_cache, "signedHeadCacheControlObserved": source.get("CacheControl"),
            "contentType": source.get("ContentType"), "contentEncoding": source.get("ContentEncoding"),
            "encodedBytes": source.get("ContentLength"),
            "metadata": {k: raw_headers.get("x-amz-meta-" + k) for k in METADATA}})
    report = {"manifestUrl": inventory["manifestUrl"], "operation": "read-only-public-and-signed-HEAD",
        "writePerformed": False, "existingObjects": len(results), "objects": results}
    write_json(args.report_dir / "cache-audit.json", report)
    print(json.dumps(report), flush=True)


def publish(args):
    inventory = checked_inventory(args)
    items = inventory["objects"]
    args.report_dir.mkdir(parents=True, exist_ok=True)
    # Inspect every destination before mutation. Matching objects support resume;
    # mismatched objects halt without overwrite, including any already live root.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda i: public_read(i, inventory, True), items))
    existing = {i["path"]: r for i, r in zip(items, results) if r}
    write_json(args.report_dir / "preflight.json", {"manifestUrl": inventory["manifestUrl"],
        "matchingExistingObjects": list(existing), "missingObjects": [i["path"] for i in items if i["path"] not in existing],
        "writePerformed": False})
    if args.dry_run:
        print(json.dumps({"dryRun": True, "manifestUrl": inventory["manifestUrl"],
            "objectCount": len(items), "matchingExistingObjects": len(existing)}), flush=True)
        return
    s3 = s3_client(args.profile) if len(existing) != len(items) else None

    def publish_one(item):
        if item["path"] in existing:
            return {**existing[item["path"]], "operation": "verified-existing"}
        encoded = contained(args.payload, item["path"]).read_bytes()
        require(sha(encoded) == item["encodedSha256"], "payload changed after preflight")
        options = {"Bucket": BUCKET, "Key": inventory["prefix"] + item["path"], "Body": encoded,
            "ContentType": item["contentType"], "CacheControl": CACHE, "IfNoneMatch": "*",
            "Metadata": {name: str(item[field]) for name, field in METADATA.items()}}
        if item["contentEncoding"]:
            options["ContentEncoding"] = item["contentEncoding"]
        try:
            s3.put_object(**options)
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code not in ("PreconditionFailed", "ConditionalRequestConflict"):
                raise
            return {**public_read(item, inventory), "operation": "verified-concurrent-existing"}
        print(json.dumps({"uploaded": item["path"], "encodedBytes": item["encodedBytes"]}), flush=True)
        return {**public_read(item, inventory), "operation": "uploaded-and-verified"}

    verified = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        for result in executor.map(publish_one, items[:-1]):
            verified.append(result)
            write_json(args.report_dir / "payload-verification.json", {"manifestPublished": False, "objects": verified})
    # Make the root live only after every dependency passed public HTTP checks.
    verified.append(publish_one(items[-1]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        final = list(executor.map(lambda i: public_read(i, inventory), items))
    report = {"format": "moenotes.game-ui-public-verification/2", "manifestUrl": inventory["manifestUrl"],
        "manifestSha256": MANIFEST_SHA, "allVerified": True, "objectCount": len(final),
        "decodedBytes": inventory["decodedBytes"], "encodedBytes": inventory["encodedBytes"],
        "manifestPublishedLast": True, "cacheControlRequested": CACHE,
        "cacheControlObserved": sorted({i["cacheControlObserved"] for i in final}),
        "cachePolicyHonored": all(i["cachePolicyHonored"] for i in final),
        "cacheObservation": "The existing delivery channel returns max-age=0 when the requested immutable policy is not honored; the cause is not established.",
        "uploads": verified, "objects": final}
    write_json(args.report_dir / "public-verification.json", report)
    print(json.dumps({"complete": True, "manifestUrl": inventory["manifestUrl"], "verifiedObjects": len(final),
        "decodedBytes": inventory["decodedBytes"], "encodedBytes": inventory["encodedBytes"]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--source", type=Path, required=True)
    prep.add_argument("--work", type=Path, required=True)
    prep.add_argument("--archive", type=Path)
    put = commands.add_parser("publish")
    put.add_argument("--payload", type=Path, required=True)
    put.add_argument("--inventory", type=Path, required=True)
    put.add_argument("--report-dir", type=Path, required=True)
    put.add_argument("--profile")
    put.add_argument("--dry-run", action="store_true")
    put.add_argument("--cache-audit-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args)
        elif args.cache_audit_only:
            cache_audit(args)
        else:
            publish(args)
    except Exception as error:
        report = {"complete": False, "errorType": type(error).__name__}
        code = getattr(error, "response", {}).get("Error", {}).get("Code")
        if code:
            report["errorCode"] = code
        if isinstance(error, ValueError):
            report["reason"] = str(error)
        if args.command == "publish":
            write_json(args.report_dir / "failure.json", report)
        print(json.dumps(report), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
