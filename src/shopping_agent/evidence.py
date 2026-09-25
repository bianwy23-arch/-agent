"""One current-turn source resolver for claims, citations, reasons and repairs.

Handles are program-issued references, never a semantic entailment guarantee.
"""
from copy import deepcopy
import hashlib
import json
from .state import InvalidChange


def source_text(value):
    return "\n".join(str(x) for x in value) if isinstance(value, list) else str(value)


def raw_source(inspected, field):
    if field == "price":
        return (inspected.get("facts", {}).get("price", {}).get("evidence") or {}).get("text")
    raw = inspected["context"]
    return raw.get("details", {}).get(field[8:]) if field.startswith("details.") else raw.get(field)


def resolve_source(turn, product_id, field):
    inspected = turn.inspected.get(product_id)
    if inspected is None:
        raise InvalidChange("source_not_inspected: inspect this product in the current turn: " + product_id)
    fact = inspected.get("facts", {}).get(field) or {}
    evidence = fact.get("evidence") or {}
    source_field = evidence.get("field") or field
    raw = raw_source(inspected, source_field)
    text = evidence.get("text") if evidence else raw
    if text is None or raw is None or not source_text(text).strip():
        raise InvalidChange("source_missing: " + product_id + ":" + field + "; use a field actually read by inspect_product")
    text = source_text(text)
    if text not in source_text(raw):
        raise InvalidChange("source_conflict: reported evidence does not occur in its original field: " + product_id + ":" + field)
    return {"product_id": product_id, "field": field, "source_field": source_field, "text": text}


def validate_citation(turn, citation):
    try:
        source = resolve_source(turn, citation.product_id, citation.field)
    except InvalidChange as exc:
        raise InvalidChange("citation requires evidence inspected this turn and a valid source field: " + str(exc)) from None
    if not citation.quote.strip() or citation.quote not in source["text"]:
        raise InvalidChange("citation quote does not occur in its source field: product=" + citation.product_id + ", field=" + citation.field + "; copy an exact source substring")
    return source


def citation_supports(turn, citation, source):
    if citation.product_id != source["product_id"]:
        return False
    cited = validate_citation(turn, citation)
    return (cited["source_field"] == source["source_field"] and
            (citation.quote in source["text"] or source["text"] in citation.quote))


def issue_evidence(turn, product_id):
    inspected = turn.inspected[product_id]
    fields = list(dict.fromkeys([*inspected.get("facts", {}), "title", "features", "description"]))
    records = []
    for field in fields:
        try:
            source = resolve_source(turn, product_id, field)
        except InvalidChange:
            continue  # Missing or conflicting evidence never receives a handle.
        identity = [turn.turn_id, turn.task_id, turn.catalog.version, source]
        ref = "ev_" + hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        turn.evidence_registry[ref] = {"source": source, "turn_id": turn.turn_id, "task_id": turn.task_id, "catalog_version": turn.catalog.version}
        records.append({"evidence_id": ref, "attribute": field, "source_field": source["source_field"],
                        "excerpt": source["text"][:320], "excerpt_truncated": len(source["text"]) > 320})
    return records


def resolve_reference(turn, ref, product_id):
    record = turn.evidence_registry.get(ref)
    if record is None:
        raise InvalidChange("evidence_ref_unknown: " + ref + "; copy evidence_id from this turn's inspect_product result; old-turn IDs are not valid")
    if (record["turn_id"], record["task_id"], record["catalog_version"]) != (turn.turn_id, turn.task_id, turn.catalog.version):
        raise InvalidChange("evidence_ref_stale: task, turn or catalog changed; inspect again")
    source = record["source"]
    if source["product_id"] != product_id:
        raise InvalidChange("evidence_ref_product_mismatch: reference must belong to the same product as the reason")
    if resolve_source(turn, product_id, source["field"]) != source:
        raise InvalidChange("evidence_ref_stale: source changed; inspect this product again")
    return deepcopy(source)


def expand_reason_references(turn, answer):
    """Adapt new handles into the existing fact/qualification pipeline, once.

Only declared reason evidence is materialized; never repairs arbitrary claims,
changes selections, or deletes invalid citations.
"""
    if not any(r.evidence_refs for r in answer.recommendation_reasons):
        return answer
    if answer.kind != "recommendation":
        raise InvalidChange("recommendation_reasons only belong to recommendation answers")
    from .contracts import Claim, Citation
    claims, citations, reasons = list(answer.claims), list(answer.citations), []
    used = {c.key for c in claims}
    generated = {}
    for reason in answer.recommendation_reasons:
        refs = list(reason.claim_refs)
        for ref in reason.evidence_refs:
            source = resolve_reference(turn, ref, reason.product_id)
            if ref not in generated:
                key = "source_" + ref
                while key in used:
                    key += "_"
                used.add(key); generated[ref] = key
                claims.append(Claim(key=key, kind="attribute_fact", product_ids=[reason.product_id], field=source["field"]))
                cite = Citation(product_id=reason.product_id, field=source["source_field"], quote=source["text"])
                if cite not in citations:
                    citations.append(cite)
            refs.append(generated[ref])
        reasons.append(reason.model_copy(update={"claim_refs": refs, "evidence_refs": []}))
    return answer.model_copy(update={"claims": claims, "citations": citations, "recommendation_reasons": reasons})
