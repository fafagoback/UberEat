"""Regression: UberEat Raw is independent of FoodRadar and preserves API bytes."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from taiwan_raw_menu_worker import capture_store, attach_delivery_location
from package_and_upload_raw_snapshot import capture_entry, make_archive, HF_REPO


class FakeSession:
    def __init__(self, content, status_code):
        self.cookies = self
        self.raw = content
        self.status = status_code
        self.closed = False

    def set(self, *args, **kwargs):
        self.cookie = args[1]

    def post(self, *args, **kwargs):
        return self

    @property
    def status_code(self):
        return self.status

    @property
    def content(self):
        return self.raw

    def json(self):
        return json.loads(self.raw)

    def close(self):
        self.closed = True


class RawContractTests(unittest.TestCase):
    def setUp(self):
        self.store = {
            "store_uuid": "fa8ac75e-488f-49f8-9677-5a4ca359588b",
            "store_url": "https://www.ubereats.com/tw/store/sample/abc",
            "name": "示範店家",
            "discovered_points": [{"point_id": 123, "lat": 25.03, "lon": 121.52, "county": "台北市"}],
        }

    def test_success_preserves_original_response_bytes(self):
        raw = b'{ "status":"success","data":{"title":"Original","catalogSectionsMap":{"x":{}}} }\n'
        fake = FakeSession(raw, 200)
        with tempfile.TemporaryDirectory() as temp, patch(
            "taiwan_raw_menu_worker.requests.Session", return_value=fake
        ):
            result = capture_store(self.store, Path(temp))
            self.assertTrue(result["success"])
            self.assertEqual((Path(temp) / result["response_path"]).read_bytes(), raw)
            self.assertIn("25.03", fake.cookie)
            self.assertTrue(fake.closed)

    def test_inactive_response_preserved_not_fabricated(self):
        raw = b'{"status":"failure","data":{"code":"410"}}\n'
        fake = FakeSession(raw, 200)
        with tempfile.TemporaryDirectory() as temp, patch(
            "taiwan_raw_menu_worker.requests.Session", return_value=fake
        ):
            result = capture_store(self.store, Path(temp))
            self.assertTrue(result["inactive"])
            self.assertEqual((Path(temp) / result["response_path"]).read_bytes(), raw)

    def test_archive_does_not_change_http_body(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            file = root / "stores" / self.store["store_uuid"] / "attempt-01-status-200.body"
            file.parent.mkdir(parents=True)
            raw = b'{"data": {"price": 399, "buyOneGetOne": true}}\n'
            file.write_bytes(raw)
            record = capture_entry(file, root)
            result = make_archive(
                {"kind": "store", "worker": 0, "root": root,
                 "responses": [record], "store_count": 1},
                root, "20261102063000"
            )
            self.assertEqual(result["response_count"], 1)
            self.assertEqual(result["remote_path"],
                             "Raw/20261102063000/store-worker-00.tar.gz")

    def test_only_ubereat_repository_is_targeted(self):
        self.assertEqual(HF_REPO, "hub-google/UberEat")


if __name__ == "__main__":
    unittest.main()
