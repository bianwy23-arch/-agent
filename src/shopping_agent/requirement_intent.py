"""Conservative pre-write checks; intent labels are model claims, not proof."""
import re
from .state import InvalidChange
from .qualification import predicate


class IntentConflict(InvalidChange):
    code = 'requirement_intent_conflict'


def clear_absence(text):
    # Deliberately narrow: reject obvious single-clause non-needs, not arbitrary
    # negation. Double negatives and mixed clauses need the model's interpretation.
    if re.search(r'[，,；;。]|但|不过|而且|同时', text):
        return False
    return bool(re.search(r'^(?:我|我们|这次|目前|现在|暂时|平时|日常)*(?:(?:不需要|不要求|无需)(?!没有|不|无)|不用(?:专门)?(?:考虑|满足|支持|具备))', text))


def validate_intent(group, operations, requirements):
    meaning = group.interpretation
    kind = meaning.kind if meaning else None
    def reject(message):
        raise IntentConflict(message + ' Correct the SAME group before writing; preserve independent valid changes.')
    if kind == 'uncertain':
        if group.action != 'clarify' or operations:
            reject('Uncertain meaning requires clarify without operations, not a guessed constraint.')
        return
    if kind == 'withdraw_requirement':
        key = meaning.existing_key
        if group.scope != 'formal':
            reject('Temporary requirement withdrawal is not supported by apply; preserve formal state and revise the temporary override through the exploration workflow.')
        if group.action != 'apply' or not key or key not in requirements:
            reject('Withdrawal needs an existing requirement key in this scope.')
        if len(operations) != 1 or operations[0] != {'target':'requirements','key':key,'value':None}:
            reject('Withdrawal may delete only the named existing requirement; split other changes.')
        return
    if meaning and meaning.existing_key is not None:
        reject('existing_key is only for withdrawal.')
    if kind == 'no_requirement' and re.search(r'必须|一定要|不能|不要没有', group.quote):
        reject('Explicit or double-negative requirement cannot be acknowledged as an absent need; split the clause or clarify.')
    if kind in {'scenario', 'no_requirement'}:
        if group.action != 'apply':
            reject('Scene/non-need must not undo or replace requirements.')
        if any(op['target'] != 'scenarios' or
               (kind == 'no_requirement' and op['value'].get('active') is not False)
               for op in operations):
            reject('A use scenario or absent need cannot create, remove or exclude a product requirement.')
    if kind in {'explicit_requirement','explicit_exclusion'}:
        if any(op['target'] != 'requirements' or op['value'] is None for op in operations):
            reject('Explicit product requirement groups may only write requirements; split withdrawal/decisions.')
    for op in operations:
        value = op.get('value')
        if op['target'] != 'requirements' or not isinstance(value,dict):
            continue
        if value.get('strength') != 'hard' or value.get('status') != 'active':
            continue
        if clear_absence(group.quote):
            reject('Absent need is not a product prohibition. Use no_requirement; to cancel an existing requirement use withdraw_requirement with its exact key.')
        pred = value.get('value')
        negative = isinstance(pred,dict) and pred.get('operator') == 'not_contains'
        unsupported = op['key'] != 'budget' and predicate(value.get('field') or op['key'], pred) is None
        if negative and kind != 'explicit_exclusion':
            reject('Negative product predicates require explicit_exclusion intent, not absence of a use case.')
        if unsupported and kind not in {'explicit_requirement','explicit_exclusion'}:
            reject('Unsupported hard predicate needs explicit product-requirement intent; use scenario/no_requirement for context, uncertain for ambiguity. Genuine unsupported requirements remain saved as unknown.')
