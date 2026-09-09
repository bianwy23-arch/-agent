from decimal import Decimal
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from shopping_agent.catalog import Catalog
from shopping_agent.state import InvalidChange, TaskStore


DATA = Path(os.environ.get("SHOPPING_CATALOG_DIR", Path(__file__).resolve().parents[1] / "data/amazon/catalog_v1"))


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog(DATA)

    def test_frozen_catalog_has_twelve_categories_of_fifty(self):
        self.assertEqual(len(self.catalog.categories), 12)
        self.assertEqual(set(self.catalog.categories.values()), {50})

    def test_price_filter_is_exhaustive_and_inclusive(self):
        all_items = self.catalog.search("headphones")["items"]
        ceiling = str(all_items[10]["price"]["amount"])
        result = self.catalog.search("headphones", budget={"amount": ceiling, "currency": "USD"})
        expected = [p["id"] for p in all_items if Decimal(str(p["price"]["amount"])) <= Decimal(ceiling)]
        self.assertEqual([p["id"] for p in result["items"]], expected)
        self.assertEqual(result["coverage"], "exhaustive_structured_filter")
        self.assertFalse(result["other_requirements_evaluated"])

    def test_lexical_empty_is_not_catalog_no_match(self):
        result = self.catalog.search("headphones", query="nonexistent_token_123456789")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["structured_match_count"], 50)
        self.assertEqual(result["coverage"], "lexical_matches")

    def test_currency_and_unknown_category_fail_explicitly(self):
        with self.assertRaises(InvalidChange):
            self.catalog.search("headphones", budget={"amount": "100", "currency": "CNY"})
        with self.assertRaises(InvalidChange):
            self.catalog.search("laptops")

    def test_evidence_missing_stays_unknown_and_results_are_detached(self):
        pid = self.catalog.search("headphones")["items"][0]["id"]
        inspected = self.catalog.inspect(pid, ["price", "independently_tested_comfort"])
        self.assertEqual(inspected["facts"]["independently_tested_comfort"]["status"], "unknown")
        inspected["facts"]["price"]["value"]["amount"] = 0
        self.assertGreater(self.catalog.inspect(pid, ["price"])["facts"]["price"]["value"]["amount"], 0)

    def test_exclusion_is_applied_to_actual_catalog_search(self):
        pid = self.catalog.search("headphones")["items"][0]["id"]
        store = TaskStore(":memory:")
        try:
            task = store.create("headphones")
            store.remember(task, pid, self.catalog.inspect(pid, ["price"])["facts"])
            store.register_turn(task, "1", "不要这个")
            store.apply_group(task, "1", "exclude", [{"target": "excluded", "key": pid, "value": True}], "不要这个")
            result = self.catalog.search("headphones", excluded_ids=store.get(task)["excluded"])
            self.assertNotIn(pid, [p["id"] for p in result["items"]])
            self.assertEqual(len(result["items"]), 49)
        finally:
            store.close()

    def test_tampered_catalog_fails_before_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp)
            shutil.copytree(DATA, dst, dirs_exist_ok=True)
            with (dst / "products.jsonl").open("a") as out:
                out.write("\n")
            with self.assertRaisesRegex(ValueError, "checksum"):
                Catalog(dst)


if __name__ == "__main__":
    unittest.main()
