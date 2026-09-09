"""Read-only frozen catalog; lexical retrieval is not semantic qualification."""

from collections import Counter
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from .state import InvalidChange, money


class Catalog:
    def __init__(self, directory):
        directory = Path(directory)
        manifest = json.loads((directory / "manifest.json").read_text())
        for name in ("products.jsonl", "source_records.jsonl", "quality_report.json"):
            if sha256((directory / name).read_bytes()).hexdigest() != manifest["files"][name]:
                raise ValueError(f"catalog checksum mismatch: {name}")
        products = [json.loads(line) for line in (directory / "products.jsonl").read_text().splitlines()]
        self._products = {p["id"]: p for p in products}
        self._sources = {}
        for line in (directory / "source_records.jsonl").read_text().splitlines():
            record = json.loads(line)
            raw = record["raw"]
            pid = "amz_" + raw["parent_asin"]
            if pid in self._sources:
                raise ValueError("duplicate source")
            self._sources[pid] = record
        if len(products) != manifest["item_count"] or len(self._products) != len(products):
            raise ValueError("invalid catalog count or duplicate IDs")
        if set(self._sources) != set(self._products):
            raise ValueError("product/source mismatch")
        self.categories = dict(Counter(p["category_id"] for p in products))
        if len(self.categories) != manifest["category_count"]:
            raise ValueError("invalid category count")
        self.version = manifest["revision"]
        for pid, p in self._products.items():
            record = self._sources[pid]
            if p["source"] != record["source"] or p["source"]["revision"] != self.version:
                raise ValueError("source provenance mismatch")
            price = p["price"]
            amount = money({"amount": str(price["amount"]), "currency": price["currency"]})
            if price["currency"] != "USD" or amount != Decimal(str(record["raw"]["price"])):
                raise ValueError("price does not match original USD value")
            for attribute in p["attributes"].values():
                evidence = attribute["evidence"]
                raw_value = self._field(record["raw"], evidence["field"])
                if raw_value is None or not evidence["text"] or evidence["text"] not in str(raw_value):
                    raise ValueError("attribute evidence does not match source")

    @staticmethod
    def _field(raw, field):
        if field.startswith("details."):
            return raw.get("details", {}).get(field[len("details."):])
        return raw.get(field)

    def search(self, category, *, budget=None, query="", excluded_ids=()):
        if category not in self.categories:
            raise InvalidChange("unsupported category")
        ceiling = None
        if budget is not None:
            ceiling = money(budget)
            if budget["currency"] != "USD":
                raise InvalidChange("USD budget required; no implicit conversion")
        if not isinstance(query, str):
            raise InvalidChange("query must be text")
        excluded = set(excluded_ids)
        if not excluded.issubset(self._products):
            raise InvalidChange("unknown excluded product")
        tokens = query.casefold().split()
        candidates = [p for p in self._products.values() if p["category_id"] == category
                      and p["id"] not in excluded
                      and (ceiling is None or Decimal(str(p["price"]["amount"])) <= ceiling)]
        matched = []
        for p in candidates:
            text = " ".join([p["title"], *p["features"], *p["description"]]).casefold()
            if tokens and not all(token in text for token in tokens):
                continue
            matched.append({"id": p["id"], "title": p["title"], "category_id": category,
                            "price": deepcopy(p["price"]), "qualification": "not_evaluated"})
        matched.sort(key=lambda p: (Decimal(str(p["price"]["amount"])), p["id"]))
        return {"items": matched, "catalog_version": self.version,
                "coverage": "lexical_matches" if tokens else "exhaustive_structured_filter",
                "category_count": self.categories[category],
                "structured_match_count": len(candidates),
                "filters": {"category": category, "budget": deepcopy(budget),
                            "excluded_ids": sorted(excluded)},
                "other_requirements_evaluated": False}

    def inspect(self, product_id, fields):
        if product_id not in self._products:
            raise InvalidChange("unknown product")
        if not isinstance(fields, list) or any(not isinstance(f, str) for f in fields):
            raise InvalidChange("fields must be a list of names")
        p = self._products[product_id]
        facts = {}
        for field in fields:
            if field == "price":
                facts[field] = {"status": "source_reported", "value": deepcopy(p["price"]),
                                "evidence": {"field": "price", "text": str(self._sources[product_id]["raw"]["price"])}}
            elif field in p["attributes"]:
                facts[field] = {"status": "source_reported", **deepcopy(p["attributes"][field])}
            else:
                facts[field] = {"status": "unknown", "value": None, "evidence": None}
        return {"product_id": product_id, "catalog_version": self.version,
                "source": deepcopy(p["source"]), "facts": facts,
                "context": {"title": p["title"], "features": deepcopy(p["features"]),
                            "description": deepcopy(p["description"]), "details": deepcopy(p["details"])},
                "qualification": "not_evaluated"}
