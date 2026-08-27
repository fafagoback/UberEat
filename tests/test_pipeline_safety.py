import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from snapshot_validation import validate_document, validate_snapshot, archive_member
from json_to_db import UberEatsDBImporter, menu_identity_keys
import taiwan_store_worker as discovery
from location_batch_scraper import convert_api_data_to_schema

BATCH = "20260827110614"


def document(slug="one", name="店家"):
    return {
        "@id": f"https://www.ubereats.com/tw/store/{slug}/uuid-{slug}",
        "name": name,
        "hasMenu": {
            "hasMenuSection": [
                {
                    "name": "主食",
                    "hasMenuItem": [
                        {"name": "餐點", "identifier": "item-1", "offers": {"price": "100", "priceCurrency": "TWD"}}
                    ]
                }
            ]
        },
        "servesCuisine": ["台式"],
        "openingHoursSpecification": [
            {"dayOfWeek": "Monday", "opens": "09:00", "closes": "21:00"}
        ]
    }


class SnapshotTests(unittest.TestCase):
    def test_short_hash_collision_is_not_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            stores = []
            for index in range(2):
                doc = document(str(index))
                stores.append({"store_url": doc["@id"]})
                Path(directory, f"{BATCH}_829fe4fb_store{index}.json").write_text(json.dumps(doc), encoding="utf-8")
            files = validate_snapshot(directory, stores, BATCH)
            self.assertEqual(len(files), 2)
            self.assertEqual(len({archive_member(path, BATCH) for path in files}), 2)

    def test_wrong_store_with_same_count_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, f"{BATCH}_one.json").write_text(json.dumps(document()), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                validate_snapshot(directory, [{"store_url": document("other")["@id"]}], BATCH)

    def test_duplicate_and_wrong_batch_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for i in range(2):
                Path(directory, f"{BATCH}_{i}.json").write_text(json.dumps(document()), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                validate_snapshot(directory, [{"store_url": document()["@id"]}], BATCH)
        with self.assertRaisesRegex(ValueError, "batch"):
            validate_document(document(), BATCH, "20260101000000_test.json")

    def test_empty_menu_requires_explicit_closed_status(self):
        doc = document()
        doc["hasMenu"]["hasMenuSection"] = []
        with self.assertRaises(ValueError):
            validate_document(doc)
        doc["isOpen"] = False
        validate_document(doc)

    def test_missing_identity_and_missing_menu_rejected(self):
        for field in ["@id", "hasMenu"]:
            doc = document()
            del doc[field]
            with self.assertRaises(ValueError):
                validate_document(doc)

    def test_explicit_empty_api_catalog_is_distinct_from_missing_catalog(self):
        url = document()["@id"]
        validate_document(convert_api_data_to_schema({"title": "Closed catalog", "catalogSectionsMap": {}}, url))
        with self.assertRaises(ValueError):
            validate_document(convert_api_data_to_schema({"title": "Incomplete response"}, url))


class DiscoveryTests(unittest.TestCase):
    def test_http_failure_is_not_an_empty_success(self):
        with patch.object(discovery.requests, "Session") as session, patch.object(discovery.time, "sleep"):
            session.return_value.post.return_value = MagicMock(status_code=500)
            with self.assertRaisesRegex(RuntimeError, "failed"):
                discovery.scan_single_point({"id": 1, "latitude": 25, "longitude": 121}, 1)

    def test_legitimate_empty_feed(self):
        with patch.object(discovery.requests, "Session") as session, patch.object(discovery.time, "sleep"):
            response = MagicMock(status_code=200)
            response.json.return_value = {"data": {"feedItems": [], "meta": {"hasMore": False}}}
            session.return_value.post.return_value = response
            self.assertEqual(discovery.scan_single_point({"id": 1, "latitude": 25, "longitude": 121}, 0), (1, [], 1))


class ProductIdentityTests(unittest.TestCase):
    def test_variants_split_but_repeated_source_item_deduplicates(self):
        a = {"name": "可樂", "identifier": "small"}
        b = {"name": "可樂", "identifier": "large"}
        key = menu_identity_keys([{"hasMenuItem": [a, b, a]}], "store")
        self.assertNotEqual(key(a), key(b))
        self.assertEqual(key(a), key(dict(a)))


if __name__ == "__main__":
    unittest.main()
