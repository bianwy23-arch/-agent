"""Customer wording over verified facts; never selects or qualifies products."""
import re
from decimal import Decimal, ROUND_HALF_UP


def customer_number(value, unit):
    """Display only; qualification and verified numeric values stay exact."""
    amount = Decimal(str(value))
    step = Decimal('0.1') if unit == 'g' else Decimal('0.01')
    rounded = amount.quantize(step, rounding=ROUND_HALF_UP)
    label = '美元' if unit == 'USD' else unit
    if amount != 0 and rounded == 0:
        return ('-' if amount < 0 else '') + '不足 ' + str(step) + ' ' + label
    text = format(rounded, 'f').rstrip('0').rstrip('.')
    return ('约 ' if rounded != amount else '') + (text or '0') + ' ' + label


def product_name(catalog, pid):
    title = catalog._products[pid]['title']
    first = title.strip()
    name = first if len(first) <= 40 else first[:37].rsplit(' ', 1)[0] + '…'
    # Similar variants must remain distinguishable; retain the source title if needed.
    if any(other != pid and p['title'].strip().startswith(name.rstrip('…'))
           for other, p in catalog._products.items()):
        return title
    return name


def field_label(field):
    from .delivery import LABELS
    aliases = {'details.Item Weight':'weight', 'details.Number of Keys':'number_of_keys',
               'details.Capacity':'capacity', 'title':'title', 'features':'features'}
    key = aliases.get(field, field or '')
    return LABELS.get(key, {'title':'商品名称', 'features':'商品说明'}.get(key, '商品信息'))


def claim_text(turn, claim):
    """Only reshape checked claims, preserving their scope and conditions."""
    text = claim['text']
    if claim['kind'] == 'attribute_fact':
        field = claim.get('field')
        name = product_name(turn.catalog, claim['product_ids'][0])
        value = text.removeprefix(name + ' 的资料标注“').removesuffix('”。')
        if field == 'title':
            return name
        if field == 'price':
            return f'{name}：历史参考价 {value} 美元。'
        return f'{name}：{field_label(field)}标注为“{value}”。'
    if claim['kind'] == 'numeric_difference':
        from .delivery import display_number
        unit = claim['unit']
        old = display_number(claim['numeric_value']) + ' ' + unit
        text = text.replace(old + '。', customer_number(claim['numeric_value'], unit) + '。')
    text = text.replace('按已记录的', '按你提出的').replace('软目标', '偏好')
    text = text.replace('；这不是硬筛选条件。', '；未达到这项偏好的商品仍可考虑。')
    text = text.replace('在已记录软偏好上互有优劣，不能伪造唯一最优。', '各有取舍，需要看你更重视哪一点。')
    text = text.replace('当前证据不足以核验', '现有资料还无法确认')
    text = text.replace('；这不等于原文或全库没有该信息，不能据此证明商品不满足。', '；暂不能判断是否满足。')
    if claim.get('field') == 'weight':
        text = '商品重量：' + text
    return text


# Exact field/value mappings only. Unrecognized source text remains verbatim;
# neither substring replacement nor inference is permitted here.
SOURCE_PHRASES = {
    'connectivity': {'wireless': '采用无线连接', 'wired': '采用有线连接',
                     'bluetooth': '支持蓝牙连接'},
    'form_factor': {'in ear': '采用入耳式设计', 'in-ear': '采用入耳式设计',
                    'over ear': '采用包耳式设计', 'over-ear': '采用包耳式设计',
                    'on ear': '采用贴耳式设计', 'on-ear': '采用贴耳式设计'},
    'material': {'nylon': '材质为尼龙', 'polyester': '材质为聚酯纤维',
                 'silicone': '材质为硅胶', 'cotton': '材质为棉'},
}
SOURCE_UNITS = {
    'weight': {'g': '克', 'gram': '克', 'grams': '克', 'kg': '千克',
               'kilogram': '千克', 'kilograms': '千克', 'ounce': '盎司',
               'ounces': '盎司', 'oz': '盎司', 'pound': '磅', 'pounds': '磅', 'lb': '磅', 'lbs': '磅'},
    'capacity': {'l': '升', 'liter': '升', 'liters': '升', 'litre': '升',
                 'litres': '升', 'ml': '毫升', 'milliliter': '毫升', 'milliliters': '毫升'},
}


def source_phrase(source):
    """A display-only translation of an entire recognized source value."""
    field, raw = source['field'], source['text']
    value = raw.strip().casefold()
    phrase = SOURCE_PHRASES.get(field, {}).get(value)
    if phrase:
        return phrase
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*([a-z]+)', value)
    if match and match[2] in SOURCE_UNITS.get(field, {}):
        label = {'weight': '商品重量', 'capacity': '容量'}[field]
        return f"{label}为 {match[1]} {SOURCE_UNITS[field][match[2]]}"
    return None


def source_sentences(sources):
    """Combine adjacent known values; preserve all unknown source blocks in order."""
    lines, phrases = [], []
    def flush():
        if phrases:
            lines.append('资料标注：' + '，'.join(phrases) + '。')
            phrases.clear()
    for source in sources:
        phrase = source_phrase(source)
        if phrase is None:
            flush()
            lines.append(f"{field_label(source['field'])}原文：{source['text']}")
        else:
            phrases.append(phrase)
    flush()
    return lines


def recommendation(turn, answer, *, include_optional_reasons=True):
    lines = ['可以先看这几款：' if len(answer.product_ids) > 1 else '可以先看这款：']
    if getattr(turn, 'quantity_summary', ''):
        lines.insert(0, turn.quantity_summary)
    reason_texts = {}
    for reason in (answer.recommendation_reasons if include_optional_reasons else []):
        reason_texts.setdefault(reason.product_id, []).append(reason.text.strip())
    for pos, pid in enumerate(answer.product_ids, 1):
        value = turn.inspected[pid]['facts']['price']['value']
        lines.append(f"{pos}. {product_name(turn.catalog, pid)} — {value['amount']} 美元")
        sources = [claim['source'] for claim in getattr(turn, '_verified_claims', [])
                   if claim['kind'] == 'attribute_fact'
                   and claim['key'].startswith('prepared_fact_')
                   and claim['product_ids'] == [pid]]
        for sentence in source_sentences(sources):
            lines.append('   ' + sentence)
        for text in reason_texts.get(pid, []):
            lines.append("   " + text)
    claims = getattr(turn, '_verified_claims', [])
    required = set((answer.recommendation_proposal.claim_refs if answer.recommendation_proposal else []))
    selected = set(answer.product_ids)
    reasons = [c for c in claims if c['kind'] != 'attribute_fact'
               and (selected.intersection(c['product_ids']) or c['key'] in required)]
    if reasons:
        lines.append('')
        for c in reasons:
            lines.append(claim_text(turn, c))
            for condition in c.get('conditions', []):
                lines.append('前提：' + condition)
    elif not reason_texts and include_optional_reasons:
        lines.append('这些商品符合你目前提出的必需条件。')
    if turn.plan.cheapest_requested:
        lines.append('价格比较限于这里收录的商品，不代表全市场最低价。')
    if re.search(r'(?:替我|直接).*(?:下单|购买)|帮我下单', turn.text):
        lines.append('我不能替你下单或付款。')
    lines.append('价格为历史参考价；商品信息来自资料标注，并非独立实测。')
    return '\n'.join(lines)
