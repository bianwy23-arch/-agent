"""Deterministic predicates over source-reported attributes, never eval labels.

Supported vocabulary is deliberately explicit. Unknown units, component scope,
ambiguous cup sizes and conflicting source values cannot pass a hard predicate.
"""
from decimal import Decimal, InvalidOperation
import re


CATEGORY_FIELDS = {
    "headphones": ["connectivity", "form_factor", "weight", "waterproof"],
    "bluetooth_speakers": ["connectivity", "speaker_type", "weight", "waterproof"],
    "keyboards": ["connectivity", "keyboard_description", "number_of_keys", "compatible_devices", "weight"],
    "mice": ["connectivity", "tracking", "number_of_buttons", "weight"],
    "rice_cookers": ["capacity", "power", "voltage", "material", "power_source"],
    "electric_kettles": ["capacity", "power", "voltage", "material", "weight"],
    "vacuum_cleaners": ["vacuum_type", "power_source", "power", "weight", "capacity", "battery_life"],
    "desk_lamps": ["light_source", "power_source", "power", "voltage", "material"],
    "backpacks": ["capacity", "material", "weight"],
    "water_bottles": ["capacity", "material", "weight"],
    "electric_toothbrushes": ["age_range", "power_source"],
    "electric_shavers": ["shaving_use", "power_source", "head_type", "battery_life"],
}

ALIASES = {"connection": "connectivity", "wattage": "power", "waterproof_rating": "waterproof", "compatibility": "compatible_devices"}
NUMERIC = {"capacity": "L", "power": "W", "voltage": "V", "weight": "g", "battery_life": "min", "number_of_keys": "count", "number_of_buttons": "count"}
UNITS = {
    "l": ("L", "1"), "liter": ("L", "1"), "liters": ("L", "1"), "litres": ("L", "1"), "升": ("L", "1"),
    "ml": ("L", ".001"), "milliliters": ("L", ".001"), "milliliter": ("L", ".001"), "毫升": ("L", ".001"),
    "w": ("W", "1"), "watt": ("W", "1"), "watts": ("W", "1"), "瓦": ("W", "1"),
    "v": ("V", "1"), "volt": ("V", "1"), "volts": ("V", "1"), "伏": ("V", "1"),
    "g": ("g", "1"), "gram": ("g", "1"), "grams": ("g", "1"), "克": ("g", "1"),
    "kg": ("g", "1000"), "kilograms": ("g", "1000"), "千克": ("g", "1000"),
    "pounds": ("g", "453.59237"), "pound": ("g", "453.59237"), "lb": ("g", "453.59237"), "lbs": ("g", "453.59237"),
    "ounces": ("g", "28.349523125"), "ounce": ("g", "28.349523125"), "oz": ("g", "28.349523125"),
    "min": ("min", "1"), "minutes": ("min", "1"), "minute": ("min", "1"), "分钟": ("min", "1"),
    "hours": ("min", "60"), "hour": ("min", "60"), "h": ("min", "60"), "小时": ("min", "60"),
    "count": ("count", "1"), "个": ("count", "1"),
}
VOCAB = {
    "connectivity": {"wired": ["wired", "有线"], "wireless": ["wireless", "bluetooth", "无线", "蓝牙", "radio frequency"], "bluetooth": ["bluetooth", "蓝牙"], "usb": ["usb"], "usb-c": ["usb-c", "usb c"], "wifi": ["wi-fi", "wifi"]},
    "form_factor": {"over_ear": ["over ear", "over-ear", "包耳", "头戴"], "in_ear": ["in ear", "in-ear", "入耳"], "on_ear": ["on ear", "on-ear", "压耳"], "one_ear": ["one-ear", "单耳"]},
    "material": {"stainless_steel": ["stainless steel", "不锈钢"], "plastic": ["plastic", "塑料"], "glass": ["glass", "玻璃"], "nylon": ["nylon", "尼龙"], "polyester": ["polyester", "聚酯"], "canvas": ["canvas", "帆布"], "leather": ["leather", "皮革"], "silicone": ["silicone", "硅胶"], "aluminum": ["aluminum", "铝"], "ceramic": ["ceramic", "陶瓷"]},
    "power_source": {"battery": ["battery powered", "电池", "lithium rechargeable", "rechargeable", "充电式"], "corded": ["corded electric", "power plug", "plug-in", "electrical cable", "插电", "ac"], "usb": ["usb"]},
    "tracking": {"optical": ["optical", "光学"], "laser": ["laser", "激光"], "trackball": ["trackball", "轨迹球"]},
    "vacuum_type": {"handheld": ["handheld", "手持"], "stick": ["stick", "杆式"], "robotic": ["robotic", "机器人"], "upright": ["upright", "立式"], "canister": ["canister", "cannister", "桶式"]},
    "light_source": {"led": ["led", "leds"], "incandescent": ["incandescent", "白炽"], "halogen": ["halogen", "卤素"], "cfl": ["cfl"]},
    "age_range": {"adult": ["adult", "成人"], "kid": ["kid", "kids", "child", "children", "儿童"], "teen": ["teen", "青少年"]},
    "shaving_use": {"face": ["face", "面部"], "beard": ["beard", "胡须"], "head": ["head", "头部"], "body": ["body", "身体"]},
    "head_type": {"foil": ["foil", "往复式"], "rotary": ["rotary", "旋转式"]},
    "speaker_type": {"outdoor": ["outdoor", "户外"], "computer": ["computer", "电脑"], "stereo": ["stereo", "立体声"], "subwoofer": ["subwoofer", "低音炮"]},
    "keyboard_description": {"mechanical": ["mechanical", "机械"], "ergonomic": ["ergonomic", "人体工学"], "qwerty": ["qwerty"], "multimedia": ["multimedia", "多媒体"]},
    "compatible_devices": {"pc": ["pc"], "laptop": ["laptop", "笔记本"], "tablet": ["tablet", "平板"], "smartphone": ["smartphone", "手机"]},
}


def tokens(field, text):
    text = str(text).casefold()
    if re.search(r"\b(?:not|no|without|non)\b|不支持|非|无此", text):
        return set()
    found = set()
    for canonical, alternatives in VOCAB.get(field, {}).items():
        for word in [canonical, *alternatives]:
            if re.search(r"(?<![a-z])" + re.escape(word.casefold()) + r"(?![a-z])", text):
                found.add(canonical)
    if field == "keyboard_description" and re.search(r"mechanical[ -](?:feel|feeling)|机械手感", text):
        found.discard("mechanical")
    return found


def numeric(field, text, source_field=""):
    text = str(text).strip()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([\w\u4e00-\u9fff]+)?(?:\s*\(AC\))?", text, re.I)
    if not match:
        return None
    unit = (match[2] or "").casefold()
    if not unit:
        implicit = {"power": "w", "voltage": "v", "number_of_keys": "count", "number_of_buttons": "count"}
        # Bare numeric power/voltage is valid only with an explicitly typed source field.
        expected = {"power": "wattage", "voltage": "voltage", "number_of_keys": "number of keys", "number_of_buttons": "number of buttons"}
        if field not in implicit or expected[field] not in source_field.casefold():
            return None
        unit = implicit[field]
    converted = UNITS.get(unit)
    if not converted or converted[0] != NUMERIC.get(field):
        return None
    value = Decimal(match[1]) * Decimal(converted[1])
    return {"value": str(value.normalize()), "unit": converted[0]}


def normalize(product, field):
    field = ALIASES.get(field, field)
    sources = []
    attr = product.get("attributes", {}).get(field)
    if attr:
        sources.append(attr["evidence"])
    if field == "waterproof":
        for key, value in product.get("details", {}).items():
            if key.casefold() in {"water resistance level", "waterproof rating", "ip rating"}:
                sources.append({"field": "details." + key, "text": str(value)})
        for key in ["title", "features", "description"]:
            value = product.get(key, [])
            for text in ([value] if isinstance(value, str) else value):
                if re.search(r"\b(?:not|no|without)\b|不支持|非防水", text, re.I):
                    continue
                for rating in re.findall(r"\bIP(?:X|[0-6])[0-9]\b", text, re.I):
                    sources.append({"field": key, "text": rating})
    if field == "form_factor":
        title = product.get("title", "")
        matched = tokens(field, title)
        if re.search(r"\bearbuds?\b", title, re.I):
            matched.add("in_ear")
        if matched:
            # Keep literal title as evidence; earbuds is an explicit in-ear form.
            sources.append({"field": "title", "text": title})
    # Detect direct title capacity contradictions only for single-volume products.
    if field == "capacity" and product.get("category_id") in {"electric_kettles", "water_bottles"}:
        for match in re.finditer(r"\b\d+(?:\.\d+)?\s*(?:liters?|litres?|milliliters?|ml|l)\b", product.get("title", ""), re.I):
            sources.append({"field": "title", "text": match[0]})
    values = []
    evidence = []
    for source in sources:
        if field in NUMERIC:
            value = numeric(field, source["text"], source["field"])
        elif field == "waterproof":
            if re.search(r"\b(?:not|no|without)\b|不支持|非防水", source["text"], re.I):
                continue
            ratings = sorted(set(re.findall(r"\bIP(?:X|[0-6])[0-9]\b", source["text"].upper())))
            value = {"value": ratings, "unit": None} if ratings else None
        else:
            recognized = tokens(field, source["text"])
            if field == "form_factor" and re.search(r"\bearbuds?\b", source["text"], re.I):
                recognized.add("in_ear")
            recognized = sorted(recognized)
            value = {"value": recognized, "unit": None} if recognized else None
        if value:
            values.append(value)
            evidence.append({**source, "normalized": value})
    if not values:
        return {"status": "unknown", "values": [], "evidence": sources, "reason": "missing_or_ambiguous_attribute"}
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    conflict = len(unique) > 1
    if field in {"waterproof", "form_factor"} and any(len(v["value"]) > 1 for v in unique):
        conflict = True
    return {"status": "conflict" if conflict else "known", "values": unique,
            "evidence": evidence, "reason": "conflicting_source_values" if conflict else "source_reported"}


def predicate(field, value):
    field = ALIASES.get(field, field)
    if isinstance(value, dict):
        if set(value) != {"operator", "value", "unit"}:
            return None
        op, expected, unit = value["operator"], value["value"], value["unit"]
    else:
        op, expected, unit = "contains", str(value), None
        if field in NUMERIC:
            m = re.fullmatch(r"\s*(>=|<=|>|<|=|至少|不超过|最多)?\s*(\d+(?:\.\d+)?)\s*([\w\u4e00-\u9fff]+)\s*", expected)
            if not m:
                return None
            op = {">=": "gte", "<=": "lte", ">": "gt", "<": "lt", "=": "eq", "至少": "gte", "不超过": "lte", "最多": "lte", None: "eq"}[m[1]]
            expected, unit = m[2], m[3]
    if op not in {"eq", "gte", "lte", "gt", "lt", "contains", "not_contains"}:
        return None
    if field in NUMERIC:
        if op not in {"eq", "gte", "lte", "gt", "lt"} or not isinstance(expected, str):
            return None
        conv = UNITS.get(str(unit).casefold())
        if not conv or conv[0] != NUMERIC[field]:
            return None
        try:
            expected = Decimal(expected) * Decimal(conv[1])
        except InvalidOperation:
            return None
        if not expected.is_finite() or expected < 0:
            return None
        return op, expected
    if op not in {"eq", "contains", "not_contains"} or unit is not None:
        return None
    if field == "material" and re.search(r"内胆|内壁|接触|纯|entirely|pure|lining|interior", str(expected), re.I):
        return None
    if field == "waterproof":
        if not re.fullmatch(r"IP(?:X|[0-6])[0-9]", str(expected).upper()):
            return None
        return op, {str(expected).upper()}
    expected = tokens(field, expected)
    return (op, expected) if expected else None


def check(product, field, requirement):
    field = requirement.get("field") or field
    canonical = ALIASES.get(field, field)
    actual = normalize(product, canonical)
    desired = predicate(canonical, requirement["value"])
    if actual["status"] == "conflict":
        return {"status": "conflict", "attribute": actual}
    if desired is None or actual["status"] != "known":
        return {"status": "unknown", "attribute": actual, "reason": "unsupported_predicate" if desired is None else actual["reason"]}
    op, expected = desired
    if canonical in NUMERIC:
        found = Decimal(actual["values"][0]["value"])
        passed = {"eq": found == expected, "gte": found >= expected, "lte": found <= expected,
                  "gt": found > expected, "lt": found < expected}[op]
    else:
        found = set(actual["values"][0]["value"])
        passed = not bool(expected & found) if op == "not_contains" else expected <= found
        if not passed and op != "not_contains" and canonical not in {"form_factor", "head_type", "tracking", "age_range", "vacuum_type"}:
            # Absence from an open-ended attribute list is not proof of absence.
            return {"status": "unknown", "attribute": actual, "reason": "requested_property_not_explicit"}
        if op == "not_contains" and passed and canonical not in {"form_factor", "head_type", "tracking", "age_range", "vacuum_type"}:
            return {"status": "unknown", "attribute": actual, "reason": "absence_not_proven"}
        # Named model compatibility cannot be reduced to generic PC/tablet tokens.
        if canonical == "compatible_devices" and re.search(r"\d", str(requirement["value"])):
            return {"status": "unknown", "attribute": actual, "reason": "specific_device_not_verified"}
    return {"status": "satisfied" if passed else "violated", "attribute": actual}
