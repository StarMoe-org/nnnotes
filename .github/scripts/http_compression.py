"""HTTP JSON compression preserves the manifest's decoded-byte identity."""
from __future__ import annotations

import gzip
import hashlib
import io
import urllib.request
import zlib

METADATA_KEYS = ("decoded-sha256", "decoded-bytes", "encoded-sha256", "encoded-bytes")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encode_json(raw: bytes) -> tuple[bytes, dict]:
    encoded = gzip.compress(raw, compresslevel=6, mtime=0)
    return encoded, {"ContentEncoding": "gzip", "Metadata": {
        "decoded-sha256": digest(raw), "decoded-bytes": str(len(raw)),
        "encoded-sha256": digest(encoded), "encoded-bytes": str(len(encoded)),
    }}


def encode_brotli(raw: bytes, quality: int = 9) -> tuple[bytes, dict]:
    import brotli
    encoded = brotli.compress(raw, quality=quality)
    return encoded, {"ContentEncoding": "br", "Metadata": {
        "decoded-sha256": digest(raw), "decoded-bytes": str(len(raw)),
        "encoded-sha256": digest(encoded), "encoded-bytes": str(len(encoded)),
    }}


def decode_content(raw: bytes, encoding: str | None) -> bytes:
    if not encoding or encoding.lower().strip() == "identity":
        return raw
    enc = encoding.lower().strip()
    if enc == "gzip":
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError, zlib.error):
            raise ValueError("invalid gzip content") from None
    if enc == "br":
        import brotli
        try:
            return brotli.decompress(raw)
        except Exception:
            raise ValueError("invalid brotli content") from None
    raise ValueError("unsupported HTTP content encoding")


def get_object(url: str, timeout: int = 300) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "moenotes-story-site (GitHub Actions)",
        "Cache-Control": "no-cache", "Accept-Encoding": "gzip, br"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return {"Body": io.BytesIO(response.read()), "ContentEncoding": response.headers.get("Content-Encoding"),
            "ContentType": response.headers.get("Content-Type"),
            "Metadata": {key: response.headers.get("x-amz-meta-" + key) for key in METADATA_KEYS}}


def verify_object(obj: dict, expected_sha256: str, *, compressed_json: bool, expected_encoding: str | None = None) -> dict:
    """Check stored bytes, encoding and decoded manifest bytes independently."""
    raw = obj["Body"].read()
    encoding = obj.get("ContentEncoding")
    if compressed_json:
        if expected_encoding is not None:
            if encoding != expected_encoding:
                raise ValueError(f"JSON object does not declare Content-Encoding: {expected_encoding}")
        elif encoding not in ("gzip", "br"):
            raise ValueError("JSON object does not declare Content-Encoding: gzip or br")
        if (obj.get("ContentType") or "").split(";", 1)[0] != "application/json":
            raise ValueError("JSON object does not declare Content-Type: application/json")
    if not compressed_json and encoding:
        raise ValueError("non-JSON object has an unexpected content encoding")
    decoded = decode_content(raw, encoding)
    if digest(decoded) != expected_sha256:
        raise ValueError("decoded object SHA-256 differs")
    facts = {"decoded-sha256": digest(decoded), "decoded-bytes": str(len(decoded)),
        "encoded-sha256": digest(raw), "encoded-bytes": str(len(raw))}
    if compressed_json and any((obj.get("Metadata") or {}).get(key) != value for key, value in facts.items()):
        raise ValueError("stored object metadata differs from encoded/decoded bytes")
    return facts
