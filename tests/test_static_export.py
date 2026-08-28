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
                scope="taiwan",
                upload_hf=False
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
                scope="taiwan",
                upload_hf=False
            )

            self.assertEqual(stats["latest_batch"], BATCH)
            self.assertEqual(stats["total_stores"], 1)
            self.assertTrue(os.path.exists(os.path.join(out_dir, "stats.json")))
            self.assertTrue(os.path.exists(os.path.join(out_dir, "discounts.json")))
            self.assertTrue(os.path.exists(os.path.join(out_dir, "products.json")))
            self.assertTrue(os.path.exists(os.path.join(out_dir, f"taiwan_catalog_{BATCH}.parquet")))


class EffectivePriceAndDiscountTests(unittest.TestCase):
    def test_calculate_effective_price_rules(self):
        from json_to_db import calculate_effective_price

        # 1. 常規商品
        self.assertEqual(calculate_effective_price(100.0, "無", 1), 100.0)

        # 2. 買 1 送 1 (付 1 件價得 2 件)
        self.assertEqual(calculate_effective_price(100.0, "買1送1", 2), 50.0)
        self.assertEqual(calculate_effective_price(90.0, "買一送一", 2), 45.0)

        # 3. 買 2 送 1 (付 2 件價得 3 件: 90 * 2 / 3 = 60，非 30)
        self.assertEqual(calculate_effective_price(90.0, "買2送1", 3), 60.0)
        self.assertEqual(calculate_effective_price(90.0, "買二送一", 3), 60.0)

        # 4. 買 3 送 1 (付 3 件價得 4 件: 100 * 3 / 4 = 75)
        self.assertEqual(calculate_effective_price(100.0, "買3送1", 4), 75.0)

        # 5. 第 2 件半價 (付 1.5 件價得 2 件: 100 * 1.5 / 2 = 75)
        self.assertEqual(calculate_effective_price(100.0, "第2件半價", 2), 75.0)
        self.assertEqual(calculate_effective_price(100.0, "第二件半價", 2), 75.0)

        # 6. 加 1 元多 1 件 ((100 + 1) / 2 = 50.5)
        self.assertEqual(calculate_effective_price(100.0, "加1元多1件", 2), 50.5)

        # 7. 組合商品 (6入組 300 元)
        self.assertEqual(calculate_effective_price(300.0, "無", 6), 50.0)

    def test_discounts_logic_with_sqlite(self):
        import sqlite3
        from export_static_snapshots import calculate_7day_discounts
        from json_to_db import calculate_effective_price

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
        CREATE TABLE crawl_batches (
            crawled_time VARCHAR(14) PRIMARY KEY,
            benchmark_address VARCHAR(255),
            benchmark_lat DECIMAL(10, 7),
            benchmark_lon DECIMAL(10, 7),
            total_discovered INT,
            success_count INT,
            fail_count INT
        );""")
        conn.execute("""
        CREATE TABLE stores (
            store_id VARCHAR(32),
            crawled_time VARCHAR(14),
            store_name VARCHAR(255),
            store_type VARCHAR(50),
            store_url VARCHAR(1000),
            rating_value DECIMAL(3, 2),
            review_count INT,
            price_range VARCHAR(10),
            telephone VARCHAR(50),
            country_code VARCHAR(10),
            region VARCHAR(50),
            locality VARCHAR(50),
            street_address VARCHAR(255),
            postal_code VARCHAR(20),
            latitude DECIMAL(10, 7),
            longitude DECIMAL(10, 7),
            order_action_url TEXT,
            total_menu_items INT,
            is_open INT,
            PRIMARY KEY (store_id, crawled_time)
        );""")
        conn.execute("""
        CREATE TABLE products (
            product_id VARCHAR(32),
            crawled_time VARCHAR(14),
            store_id VARCHAR(32),
            store_name VARCHAR(255),
            category_name VARCHAR(100),
            product_name VARCHAR(255),
            price DECIMAL(10, 2),
            currency VARCHAR(10),
            description TEXT,
            promo_type VARCHAR(50),
            quantity INT,
            eff_price DECIMAL(10, 2),
            is_open INT,
            PRIMARY KEY (product_id, crawled_time)
        );""")

        t_prev = "20260827100000"
        t_curr = "20260828100000"

        conn.execute("INSERT INTO crawl_batches VALUES (?, 'TW', 25.0, 121.5, 1, 1, 0)", (t_prev,))
        conn.execute("INSERT INTO crawl_batches VALUES (?, 'TW', 25.0, 121.5, 1, 1, 0)", (t_curr,))
        conn.execute("INSERT INTO stores VALUES ('s1', ?, '測試店', 'Rest', 'http://store', 4.5, 100, '$', '123', 'TW', 'Taipei', 'Xinyi', 'Road', '110', 25.0, 121.0, 'http://order', 4, 1)", (t_prev,))
        conn.execute("INSERT INTO stores VALUES ('s1', ?, '測試店', 'Rest', 'http://store', 4.5, 100, '$', '123', 'TW', 'Taipei', 'Xinyi', 'Road', '110', 25.0, 121.0, 'http://order', 4, 1)", (t_curr,))

        # 商品 1: 前天買2送1 ($90/3 -> $60)，今天也是買2送1 ($90/3 -> $60) -> 0% 變動，不應出現在降價清單
        p1_eff_prev = calculate_effective_price(90.0, "買2送1", 3)
        p1_eff_curr = calculate_effective_price(90.0, "買2送1", 3)
        conn.execute("INSERT INTO products VALUES ('p1', ?, 's1', '測試店', '咖啡', '美式咖啡', 90.0, 'TWD', '', '買2送1', 3, ?, 1)", (t_prev, p1_eff_prev))
        conn.execute("INSERT INTO products VALUES ('p1', ?, 's1', '測試店', '咖啡', '美式咖啡', 90.0, 'TWD', '', '買2送1', 3, ?, 1)", (t_curr, p1_eff_curr))

        # 商品 2: 前天原價 $90，今天買2送1 ($90/3 -> $60) -> 降 33.3%，現省 $30，應出現在降價清單
        p2_eff_prev = calculate_effective_price(90.0, "無", 1)
        p2_eff_curr = calculate_effective_price(90.0, "買2送1", 3)
        conn.execute("INSERT INTO products VALUES ('p2', ?, 's1', '測試店', '咖啡', '拿鐵咖啡', 90.0, 'TWD', '', '無', 1, ?, 1)", (t_prev, p2_eff_prev))
        conn.execute("INSERT INTO products VALUES ('p2', ?, 's1', '測試店', '咖啡', '拿鐵咖啡', 90.0, 'TWD', '', '買2送1', 3, ?, 1)", (t_curr, p2_eff_curr))

        # 商品 3: 店家漏打 0 手誤 (前天 $286，今天 $28，無促銷標籤) -> 降幅 90.2%，應被防呆過濾
        p3_eff_prev = calculate_effective_price(286.0, "無", 1)
        p3_eff_curr = calculate_effective_price(28.0, "無", 1)
        conn.execute("INSERT INTO products VALUES ('p3', ?, 's1', '測試店', '主食', '牛肉麵', 286.0, 'TWD', '', '無', 1, ?, 1)", (t_prev, p3_eff_prev))
        conn.execute("INSERT INTO products VALUES ('p3', ?, 's1', '測試店', '主食', '牛肉麵', 28.0, 'TWD', '', '無', 1, ?, 1)", (t_curr, p3_eff_curr))

        # 商品 4: 打烊狀態 (is_open = 0) -> 不應被比對
        p4_eff_prev = calculate_effective_price(100.0, "無", 1)
        p4_eff_curr = calculate_effective_price(50.0, "買1送1", 2)
        conn.execute("INSERT INTO products VALUES ('p4', ?, 's1', '測試店', '點心', '蛋糕', 100.0, 'TWD', '', '無', 1, ?, 0)", (t_prev, p4_eff_prev))
        conn.execute("INSERT INTO products VALUES ('p4', ?, 's1', '測試店', '點心', '蛋糕', 50.0, 'TWD', '', '買1送1', 2, ?, 1)", (t_curr, p4_eff_curr))

        conn.commit()

        discounts = calculate_7day_discounts(conn, t_curr, min_discount_pct=20.0, min_savings_twd=20.0)

        # 驗證: 只有 商品 2 (拿鐵咖啡) 符合降價且通過所有過濾
        self.assertEqual(len(discounts), 1)
        d = discounts[0]
        self.assertEqual(d["product_id"], "p2")
        self.assertEqual(d["original_price"], 90.0)
        self.assertEqual(d["current_price"], 60.0)
        self.assertEqual(d["discount_pct"], 33.3)
        self.assertEqual(d["savings_amount"], 30.0)


if __name__ == "__main__":
    unittest.main()
