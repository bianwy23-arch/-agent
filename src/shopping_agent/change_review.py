"""Bounded reassessment of previously displayed candidates after requirement changes."""


def change_review(turn, scope="formal"):
    before = getattr(turn, "requirements_before", None)
    if before is None or scope != "formal":
        return {}
    current = turn.requirements(scope)
    def meaning(req):
        return {k:v for k,v in req.items() if k not in {"source", "id"}}
    changed = sorted({(current.get(k) or before.get(k) or {}).get("field") or k
                      for k in set(before) | set(current)
                      if meaning(before.get(k, {})) != meaning(current.get(k, {}))})
    if not changed:
        return {}
    displays = list((turn.display_snapshot or {}).values())
    latest = displays[-1] if displays else []
    historical = list(dict.fromkeys(pid for ids in displays for pid in ids))
    # Consider previously omitted alternatives first, bounded to avoid catalog-wide work.
    ordered = [p for p in historical if p not in latest] + list(latest)
    state = turn.state()
    allowed = [p for p in ordered if p in state['candidates'] and p not in state['excluded']
               and (turn.plan.scope_ids is None or p in turn.plan.scope_ids)]
    candidates = []
    for pid in allowed[:10]:
        q = turn.catalog.qualification(pid, current)
        status = "violated" if q['violated'] else "unknown" if q['unknown'] or q['conflict'] else "satisfied"
        candidates.append({"product_id":pid,"qualification":status,"previously_omitted":pid not in latest,
                           "current_source_read":pid in turn.inspected})
    return {"changed_fields":changed,"historical_candidates":candidates,"deferred_count":max(0,len(allowed)-10),
            "instruction":"需求已变化。重新考虑以前展示、后来未入选的合格商品；它们不是被排除项。先assess_candidates，程序会重新读取这些历史候选的当前冻结资料。不要因上一轮便宜商品已够数就直接结束。门槛已满足时不再把更低价格作为隐含优胜理由；可以保持结果，但须区分未入选与不合格。"}


def refresh_changed_candidates(turn, product_ids, scope):
    review = change_review(turn, scope)
    refreshed = []
    for candidate in review.get('historical_candidates', []):
        pid = candidate['product_id']
        if pid not in product_ids or pid in turn.inspected or candidate['qualification'] != 'satisfied':
            continue
        fields = list(dict.fromkeys(['price', *turn.state()['candidates'][pid].get('facts', {}),
                     *(req.get('field') or key for key, req in turn.requirements(scope).items())]))
        # This is a fresh local catalog read, not trusting model text or cached claims.
        evidence = turn.catalog.inspect(pid, fields)
        turn.inspected[pid] = evidence
        turn.store.remember(turn.task_id, pid, evidence['facts'])
        refreshed.append(pid)
    if refreshed:
        turn.events.append({'event':'changed_candidate_source_refresh','product_ids':refreshed,
                            'source':'current_frozen_catalog','scope':scope})
    return refreshed


def render_change_review(turn, answer):
    review = change_review(turn, answer.scope)
    if not review or not review.get('historical_candidates'):
        return ''
    from .delivery import LABELS
    from .presentation import product_name
    lines = ['已按你的新要求调整：']
    for key, req in turn.requirements(answer.scope).items():
        field = req.get('field') or key
        if field not in review['changed_fields']:
            continue
        if key == 'budget' and req.get('status') == 'active':
            lines.append(f"- 预算上限为 {req['value']['amount']} {req['value']['currency']}。")
        if req.get('strength') == 'soft' and req.get('status') == 'active' and (req.get('expression') or {}).get('kind') == 'target':
            lines.append('- '+LABELS.get(field,'该维度')+'达到你的目标即可，不会继续追求更低或更高。')
    omitted = [c for c in review['historical_candidates'] if c['product_id'] not in answer.product_ids]
    for c in omitted:
        name = product_name(turn.catalog, c['product_id'])
        reason = {'satisfied':'仍符合要求，也可以考虑。',
                  'violated':'不符合新的必需条件，这次先不推荐。',
                  'unknown':'还无法确认是否满足新的要求，暂不推荐。'}[c['qualification']]
        lines.append('- '+name+'：'+reason)
    if review['deferred_count']:
        lines.append(f"- 更早展示的 {review['deferred_count']} 款尚未逐项复查；以上说明仅覆盖本轮复查范围。")
    return '\n'.join(lines)
