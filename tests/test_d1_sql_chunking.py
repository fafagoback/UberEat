import os
import tempfile
import unittest

from scr.json_to_cf_d1 import build_batch_insert, split_sql_files, sql_utf8_size


def escape_sql(value):
    return "'" + str(value).replace("'", "''") + "'"


class D1SqlChunkingTests(unittest.TestCase):
    def test_insert_batches_obey_utf8_byte_limit(self):
        rows = [(index, "中" * 120) for index in range(20)]
        statements = build_batch_insert(
            "products", ["id", "description"], rows, escape_sql,
            batch_size=50, max_statement_bytes=1024,
        )
        self.assertGreater(len(statements), 1)
        self.assertTrue(all(sql_utf8_size(sql) <= 1024 for sql in statements))
        self.assertEqual(sum(sql.count("中" * 120) for sql in statements), len(rows))

    def test_single_oversized_row_fails_before_upload(self):
        with self.assertRaisesRegex(ValueError, "products.*單筆 INSERT"):
            build_batch_insert(
                "products", ["description"], [("中" * 500,)], escape_sql,
                max_statement_bytes=256,
            )

    def test_sql_files_obey_file_byte_limit(self):
        statements = ["INSERT INTO t VALUES ('" + ("中" * 1000) + "');" for _ in range(700)]
        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = os.path.join(temp_dir, "part")
            paths = split_sql_files(statements, prefix)
            self.assertGreater(len(paths), 1)
            self.assertTrue(all(os.path.getsize(path) <= 1024 * 1024 for path in paths))


if __name__ == "__main__":
    unittest.main()
