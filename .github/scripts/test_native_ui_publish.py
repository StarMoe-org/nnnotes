"""Exercise upload ordering, safe resume, and HTTP rejection using real package bytes."""
import argparse
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class Response:
    def __init__(self, body, headers):
        self.body, self.headers, self.status = body, headers, 200

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.args = SimpleNamespace(payload=BUNDLE / "encoded-payload",
            inventory=BUNDLE / "publication-inventory.json", report_dir=Path(self.temp.name),
            profile=None, dry_run=False)
        self.inventory = publisher.checked_inventory(self.args)
        self.objects, self.puts = {}, []
        self.bad_dependency = False
        self.gateway_cache_mode = False

    def object_for(self, item):
        headers = {"Content-Type": item["contentType"], "Cache-Control": publisher.CACHE,
                   "Access-Control-Allow-Origin": "*"}
        if item["contentEncoding"]:
            headers["Content-Encoding"] = item["contentEncoding"]
        headers.update({"x-amz-meta-" + k: str(item[v]) for k, v in publisher.METADATA.items()})
        return (publisher.contained(self.args.payload, item["path"]).read_bytes(), headers)

    def get(self, request, timeout):
        self.assertEqual(request.get_header("Origin"), publisher.ORIGIN)
        relative = request.full_url.split(self.inventory["prefix"], 1)[1]
        if relative not in self.objects:
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
        body, headers = self.objects[relative]
        if self.gateway_cache_mode:
            headers = {**headers, "Cache-Control": "max-age=0"}
        return Response(body, headers)

    def put_object(self, **options):
        self.assertEqual(options["Bucket"], publisher.BUCKET)
        self.assertEqual(options["IfNoneMatch"], "*")
        relative = options["Key"].removeprefix(self.inventory["prefix"])
        self.assertNotIn(relative, self.objects)
        if relative == "manifest.json":
            self.assertEqual(len(self.objects), 16, "root must follow all verified dependencies")
        self.puts.append(relative)
        headers = {"Content-Type": options["ContentType"], "Cache-Control": options["CacheControl"],
                   "Access-Control-Allow-Origin": "*"}
        if "ContentEncoding" in options:
            headers["Content-Encoding"] = options["ContentEncoding"]
        headers.update({"x-amz-meta-" + k: v for k, v in options["Metadata"].items()})
        if self.bad_dependency and relative == "index.json":
            headers["Cache-Control"] = "no-cache"
        self.objects[relative] = (options["Body"], headers)

    def run_publication(self):
        with patch.object(publisher.urllib.request, "urlopen", self.get), \
                patch.object(publisher, "s3_client", return_value=self) as client, \
                contextlib.redirect_stdout(io.StringIO()):
            publisher.publish(self.args)
        return client

    def test_manifest_last_and_complete_http_verification(self):
        self.run_publication()
        self.assertEqual(len(self.puts), 17)
        self.assertEqual(self.puts[-1], "manifest.json")
        report = json.loads((self.args.report_dir / "public-verification.json").read_text())
        self.assertTrue(report["allVerified"])
        self.assertEqual(report["encodedBytes"], 867011)

    def test_matching_partial_publication_resumes_without_rewrites(self):
        for item in self.inventory["objects"][:4]:
            self.objects[item["path"]] = self.object_for(item)
        self.run_publication()
        self.assertEqual(len(self.puts), 13)
        self.assertEqual(self.puts[-1], "manifest.json")

    def test_mismatched_existing_object_stops_before_any_mutation(self):
        item = self.inventory["objects"][0]
        body, headers = self.object_for(item)
        self.objects[item["path"]] = (body + b"corrupt", headers)
        with self.assertRaises(ValueError), patch.object(publisher, "s3_client") as client:
            self.run_publication()
        self.assertEqual(self.puts, [])
        client.assert_not_called()

    def test_failed_dependency_http_check_prevents_manifest_publication(self):
        self.bad_dependency = True
        with self.assertRaises(ValueError):
            self.run_publication()
        self.assertNotIn("manifest.json", self.objects)
        self.assertFalse((self.args.report_dir / "public-verification.json").exists())

    def test_dry_run_does_not_initialize_write_identity(self):
        self.args.dry_run = True
        client = self.run_publication()
        client.assert_not_called()
        self.assertEqual(self.puts, [])

    def test_known_delivery_cache_limitation_is_reported_without_claiming_immutable_cache(self):
        self.gateway_cache_mode = True
        self.run_publication()
        report = json.loads((self.args.report_dir / "public-verification.json").read_text())
        self.assertTrue(report["allVerified"])
        self.assertFalse(report["cachePolicyHonored"])
        self.assertEqual(report["cacheControlRequested"], publisher.CACHE)
        self.assertEqual(report["cacheControlObserved"], ["max-age=0"])
        self.assertTrue(all(i["cacheControlObserved"] == "max-age=0" and not i["cachePolicyHonored"]
                            for i in report["objects"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publisher", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    BUNDLE = args.bundle.resolve()
    spec = importlib.util.spec_from_file_location("native_ui_publisher", args.publisher)
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    unittest.main(argv=[__file__])
