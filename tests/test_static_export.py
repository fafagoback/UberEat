import json
import os
from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from export_static_snapshots import export_all_static_snapshots


BATCH = "20260827110614"

def make_sample_doc(slug="test-store", name="測試店家", item_price=100, promo_type="無", quantity=1):
    return {
        "@context": "http://schema.org",
        "@type": "Restaurant",
        "@id": f"https://www.ubereats.com/tw/store/{slug}/uuid-{slug}",
        "name": name,
        "isOpen": True,
        "telephone": "+886212345678",
        "workingHoursTagline": "10:00 - 22:00",
        "closedMessage": "",
        "hasStorePromotion": False,
        "address": {
            "@type": "PostalAddress",
            "streetAddress": "信義路五段7號",
            "addressLocality": "信義區",
            "addressRegion": "台北市",
            "postalCode": "110",
            "addressCountry": "TW"
        },
        "geo": {
            "@type": "GeoCoordinates",
            "latitude": 25.0339,
            "longitude": 121.5645
        },
        "aggregateRating": {
            "@type": "AggregateRating",
            "ratingValue": 4.8,
            "reviewCount": 350
        },
        "servesCuisine": ["美式", "漢堡"],
        "openingHoursSpecification": [
            {
                "@type": "OpeningHoursSpecification",
                "dayOfWeek": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                "opens": "10:00:00",
                "closes": "22:00:00"
            }
        ],
        "hasMenu": {
            "@type": "Menu",
            "hasMenuSection": [
                {
                    "@type": "MenuSection",
                    "name": "人氣精選",
                    "hasMenuItem": [
                        {
                            "@type": "MenuItem",
                            "name": "經典大漢堡",
                            "identifier": "item-1",
                            "description": "特製牛肉堡",
                            "offers": {
                                "@type": "Offer",
                                "price": str(item_price),
                                "priceCurrency": "TWD"
                            }
                        },
                        {
                            "@type": "MenuItem",
                            "name": "買一送一可樂",
                            "identifier": "item-2",
                            "description": "清涼可口",
                            "offers": {
                                "@type": "Offer",
                                "price": "50",
                                "priceCurrency": "TWD"
                            }
                        }
                    ]
                }
            ]
        }
    }


class StaticExportTests(unittest.TestCase):
    def test_export_all_static_snapshots_generates_all_files(self):
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as out_dir:
            # 準備兩家店的快照 JSON
            doc1 = make_sample_doc("store-1", "麥當勞台北館前店")
            doc2 = make_sample_doc("store-2", "肯德基信義店")
            # 在 doc2 中標註買一送一
            doc2["hasMenu"]["hasMenuSection"][0]["hasMenuItem"][1]["name"] = "【買1送1】可樂分享杯"

            Path(src_dir, f"{BATCH}_829fe4fb_store1.json").write_text(json.dumps(doc1, ensure_ascii=False), encoding="utf-8")
            Path(src_dir, f"{BATCH}_738ae12c_store2.json").write_text(json.dumps(doc2, ensure_ascii=False), encoding="utf-8")

            db_path = os.path.join(out_dir, "test.db")
            stats = export_all_static_snapshots(
                src_dir=src_dir,
                output_dir=out_dir,
                db_path=db_path,
                batch_id=BATCH,
                scope="taiwan"
            )

            self.assertEqual(stats["latest_batch"], BATCH)
            self.assertEqual(stats["total_stores"], 2)
            self.assertGreaterEqual(stats["total_products"], 4)

            # 驗證所有靜態檔案皆存在且非空
            expected_files = [
                "stats.json",
                "discounts.json",
                "new_stores.json",
                "new_products.json",
                "promotions.json",
                "products.json",
                "history.json",
                "version.json"
            ]

            for filename in expected_files:
                target = os.path.join(out_dir, filename)
                self.assertTrue(os.path.exists(target), f"缺少檔案: {filename}")
                self.assertGreater(os.path.getsize(target), 0, f"檔案大小為 0: {filename}")

            # 驗證 stats.json 內容結構
            stats_json = json.loads(Path(out_dir, "stats.json").read_text(encoding="utf-8"))
            self.assertEqual(stats_json["status"], "success")
            self.assertEqual(stats_json["latest_batch"], BATCH)

            # 驗證 products.json 內容結構
            products_json = json.loads(Path(out_dir, "products.json").read_text(encoding="utf-8"))
            self.assertEqual(products_json["status"], "success")
            self.assertGreaterEqual(len(products_json["items"]), 4)

            # 驗證 history.json 內容結構
            history_json = json.loads(Path(out_dir, "history.json").read_text(encoding="utf-8"))
            self.assertIn("history", history_json)

            # 驗證 Parquet 檔案生成與欄位結構
            parquet_path = os.path.join(out_dir, f"taiwan_catalog_{BATCH}.parquet")
            self.assertTrue(os.path.exists(parquet_path), "缺少 Parquet 資料湖檔案")
            import pyarrow.parquet as pq
            table = pq.read_table(parquet_path)
            self.assertGreaterEqual(table.num_rows, 4)
            columns = table.column_names
            for col in ["product_id", "store_id", "product_name", "price", "city", "locality", "eff_price"]:
                self.assertIn(col, columns, f"Parquet 缺少核心欄位: {col}")

    def test_export_all_static_snapshots_default_temp_db(self):
        """測試不傳入 db_path (預設安全暫存資料庫) 的完整流程"""
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as out_dir:
            doc1 = make_sample_doc("store-1", "麥當勞台北館前店")
            Path(src_dir, f"{BATCH}_829fe4fb_store1.json").write_text(json.dumps(doc1, ensure_ascii=False), encoding="utf-8")

            stats = export_all_static_snapshots(
                src_dir=src_dir,
                output_dir=out_dir,
                batch_id=BATCH,
                scope="taiwan"
            )

            self.assertEqual(stats["latest_batch"], BATCH)
            self.assertEqual(stats["total_stores"], 1)
            self.assertTrue(os.path.exists(os.path.join(out_dir, "stats.json")))
            self.assertTrue(os.path.exists(os.path.join(out_dir, "discounts.json")))
            self.assertTrue(os.path.exists(os.path.join(out_dir, "products.json")))
            self.assertTrue(os.path.exists(os.path.join(out_dir, f"taiwan_catalog_{BATCH}.parquet")))


if __name__ == "__main__":
    unittest.main()
