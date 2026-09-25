"""Validated tool contracts. Service-owned IDs cannot be supplied by the model."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Money(Contract):
    amount: str
    currency: Literal["USD", "CNY"]


class Predicate(Contract):
    operator: Literal["eq", "gte", "lte", "gt", "lt", "contains", "not_contains"]
    value: str
    unit: str | None = None


class Requirement(Contract):
    field: str | None = Field(default=None, description="Canonical product attribute, independent of the requirement key. Use budget only with key budget.")
    status: Literal["active", "no_preference"]
    strength: Literal["hard", "soft"]
    value: Money | Predicate | str | None


class SoftExpression(Contract):
    kind: Literal["minimize", "maximize", "target", "prefer_value", "qualitative"]
    target: Predicate | str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def expression_shape(self):
        if self.kind == "target" and not isinstance(self.target, Predicate):
            raise ValueError("target requires a predicate")
        if self.kind == "prefer_value" and (not isinstance(self.target, str) or not self.target.strip()):
            raise ValueError("prefer_value requires a canonical value")
        if self.kind in {"minimize", "maximize", "qualitative"} and self.target is not None:
            raise ValueError("unexpected target")
        if self.kind == "qualitative" and (not self.text or not self.text.strip()):
            raise ValueError("qualitative requires original meaning")
        return self


class SoftPreference(Contract):
    field: str = Field(min_length=1)
    status: Literal["active", "no_preference"] = "active"
    strength: Literal["soft"] = "soft"
    expression: SoftExpression | None

    @model_validator(mode="after")
    def status_shape(self):
        from .qualification import ALIASES
        self.field = ALIASES.get(self.field, self.field)
        if (self.status == "no_preference") != (self.expression is None):
            raise ValueError("only no_preference has null expression")
        return self


class PreferenceRelation(Contract):
    higher_preference_id: str
    lower_preference_id: str
    mode: Literal["strict", "emphasis"] = "emphasis"


class DecisionInput(Contract):
    product_ids: list[str] = Field(default_factory=list)
    display_id: str | None = None
    positions: list[StrictInt] = Field(default_factory=list)


class ScenarioValue(Contract):
    active: bool
    text: str = Field(min_length=1)


class HypothesisProposal(Contract):
    scenario_keys: list[str] = Field(min_length=1)
    field: str = Field(min_length=1)
    expression: SoftExpression
    reason: str = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)


class QuestionResponse(Contract):
    question_key: str
    outcome: Literal["answered", "unknown", "no_preference", "direct_recommendation"]
    quote: str = Field(min_length=1)


class ActionIntent(Contract):
    gap_id: str = Field(min_length=1)
    action: Literal["search", "inspect", "delivery_revalidation"]
    expected_information: str = Field(min_length=1)
    direction: str | None = None


class Operation(Contract):
    target: Literal["requirements", "excluded", "decision", "relations", "hypotheses", "scenarios"]
    key: str
    value: Requirement | SoftPreference | PreferenceRelation | DecisionInput | ScenarioValue | HypothesisProposal | bool | None

    @model_validator(mode="after")
    def target_shape(self):
        if self.target=='requirements' and self.key in {'scope','scope_ids','quantity','count','selection','focus','shortlist','decision','scenario','hypothesis'}:
            raise ValueError('interaction metadata is not a product requirement: use TurnPlan.scope_ids for existing-product scope, and decision action keys for explicit user choices; do not create a hard requirement for scope or quantity')
        if self.target == 'decision':
            allowed = {'shortlist_add','shortlist_remove','focus_set','select_tentative','select_confirmed','selection_clear','exclude','restore'}
            if self.key not in allowed:
                raise ValueError('decision key must be an action: '+', '.join(sorted(allowed))+'. selection is a state field, not an action.')
            if not isinstance(self.value,DecisionInput):
                raise ValueError('decision value must be {product_ids:[],display_id:null,positions:[]}')
        if self.target=='scenarios' and not isinstance(self.value,ScenarioValue):
            raise ValueError('scenarios value must be {active:bool,text:user_quote}')
        if self.target=='hypotheses' and not isinstance(self.value,(bool,HypothesisProposal)):
            raise ValueError('hypotheses value must be true, false or a sourced HypothesisProposal')
        return self



class RequirementInterpretation(Contract):
    kind: Literal["scenario", "no_requirement", "explicit_requirement", "explicit_exclusion", "withdraw_requirement", "uncertain"]
    existing_key: str | None = Field(default=None, description="Exact existing requirement key for withdrawal only. Other independent requirements must remain unchanged.")


class Group(Contract):
    interpretation: RequirementInterpretation | None = Field(default=None, description="Meaning of this single quoted clause. Absence of a need is not a prohibition; use separate groups for different meanings. Required for negative or unsupported hard predicates.")
    group_id: str
    quote: str
    action: Literal["apply", "undo", "clarify", "cancel_clarification", "explore", "end_exploration"]
    operations: list[Operation] = Field(default_factory=list)
    scope: Literal["formal", "hypothetical"] = "formal"
    dependent: bool = False
    undo_field: str | None = None
    clarification_fields: list[str] = Field(default_factory=list)
    pending_turn_id: str | None = None
    pending_group_id: str | None = None


class DirectionRejection(Contract):
    direction: Literal['raise_budget']
    quote: str = Field(min_length=1)


StateFactKey = Literal["preferences", "excluded", "shortlist", "selection", "selection_qualification", "budget", "purchase", "formal_budget", "temporary_budget", "exploration_status"]


class StateQuery(Contract):
    task_id: str | None = None
    category: str | None = None
    scope: Literal["formal", "hypothetical"] = "formal"
    fields: list[StateFactKey] = Field(min_length=1, max_length=11)

    @model_validator(mode="after")
    def valid_query(self):
        if not self.task_id and not self.category:
            raise ValueError("state query requires task_id or category")
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("state query fields must be unique")
        return self


class TurnPlan(Contract):
    state_queries: list[StateQuery] = Field(default_factory=list, max_length=12)
    state_query_mode: Literal["only", "alongside"] | None = None
    category: str | None = None
    new_task: bool = False
    resume_task_id: str | None = None
    groups: list[Group] = Field(default_factory=list)
    scope_ids: list[str] | None = None
    stop_requested: bool = False
    cheapest_requested: bool = False
    recommendation_scope: Literal["formal", "hypothetical"] | None = Field(default=None, description="Override recommendation scope only when the user explicitly asks for formal or temporary recommendations. Omit to continue the active valid temporary exploration, otherwise formal. Does not change state-query scope.")
    question_response: QuestionResponse | None = None
    direction_rejections: list[DirectionRejection] = Field(default_factory=list)

    @model_validator(mode="after")
    def distinct_groups(self):
        if bool(self.state_queries) != (self.state_query_mode is not None):
            raise ValueError("state_queries and state_query_mode must be supplied together")
        if len({g.group_id for g in self.groups}) != len(self.groups):
            raise ValueError("group IDs must be unique within the turn")
        if self.new_task and self.resume_task_id:
            raise ValueError("cannot create and resume a task together")
        return self


class Citation(Contract):
    product_id: str
    field: str
    quote: str


class Claim(Contract):
    key: str = Field(min_length=1)
    kind: Literal["attribute_fact", "numeric_difference", "preference_advantage", "tradeoff", "conditional_recommendation", "evidence_gap"]
    product_ids: list[str] = Field(min_length=1)
    field: str | None = None
    preference_id: str | None = None
    hypothesis_id: str | None = None
    conditions: list[str] = Field(default_factory=list)


class RecommendationProposal(Contract):
    assessment_id: str
    ordered_ids: list[str] = Field(min_length=1)
    representative_roles: dict[str, str] = Field(default_factory=dict)
    emphasis_reasons: dict[str, Literal["advantage", "tradeoff", "equal", "unknown", "strict"]] = Field(default_factory=dict)
    preference_refs: list[str] = Field(default_factory=list)
    hypothesis_refs: list[str] = Field(default_factory=list)
    claim_refs: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class Question(Contract):
    gap_id: str = Field(min_length=1)
    action: Literal["ask_user"] = "ask_user"
    expected_information: str = Field(min_length=1)


class RecommendationReason(Contract):
    product_id: str
    claim_refs: list[str] = Field(default_factory=list, max_length=4, description="Legacy or computed claim keys. Attribute reasons should prefer evidence_refs from inspect_product.")
    evidence_refs: list[str] = Field(default_factory=list, max_length=4, description="Program-issued evidence_id values from this turn's inspection of this same product. No need to repeat their attribute claims or citations. References establish provenance, not semantic truth.")
    text: str = Field(min_length=1, max_length=180, description="One concise customer-facing reason grounded in referenced evidence and current needs. Unconfirmed usage must be conditional; do not invent performance or user preferences.")


    @model_validator(mode="after")
    def bounded_references(self):
        if not 1 <= len(self.claim_refs) + len(self.evidence_refs) <= 4:
            raise ValueError("a reason needs 1–4 claim_refs/evidence_refs in total")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("evidence_refs must be distinct")
        return self


class AnswerBoundary(Contract):
    """A question-bound limitation, not a claim that a product meets a criterion."""
    question_quote: str = Field(min_length=2, max_length=300)
    target_field: str = Field(min_length=1)
    basis_fields: list[str] = Field(default_factory=list, max_length=4)
    product_ids: list[str] = Field(default_factory=list, max_length=10)


class FinalAnswer(Contract):
    answer_boundary: AnswerBoundary | None = None
    answer_purpose: Literal["interaction", "state", "evidence"] | None = Field(default=None, description="For answered only: interaction is a brief conversation-only reply without product assertions or claims of state changes; state confirms authoritative saved state; evidence answers product/concept/capability questions using verified evidence. Omit only for legacy callers.")
    recommendation_reasons: list[RecommendationReason] = Field(default_factory=list, description="Optional evidence-linked reasons, at most two per recommended product. Omit when no useful supported reason is available.")
    limit_fact_refs: list[Literal["realtime_price", "realtime_stock"]] = Field(default_factory=list)
    decision_receipt_refs: list[str] = Field(default_factory=list, description="Receipt IDs from this turn explicitly reviewed before completion. Required for no-change or partially effective decision operations. Correct wrong targets using a NEW group_id; references acknowledge evidence, not semantic success.")
    state_fact_refs: list[StateFactKey] = Field(default_factory=list)
    kind: Literal["answered", "recommendation", "needs_user", "no_match", "data_limited", "execution_limited", "stopped"]
    response_text: str | None = Field(default=None, max_length=6000, description="Optional natural final response, reviewed against verified facts before delivery. Does not change state.")
    message: str
    product_ids: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    scope: Literal["formal", "hypothetical"] = "formal"
    assessment_id: str | None = None
    recommendation_proposal: RecommendationProposal | None = None
    claims: list[Claim] = Field(default_factory=list)
    question: Question | None = None
    explanation_topics: list[Literal["hard_vs_soft", "unknown_vs_no_preference", "selection_vs_purchase", "historical_prices"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def proposal_matches_assessment(self):
        # The proposal already names an explicit assessment. Canonicalize a
        # missing duplicate without guessing a latest assessment or bypassing
        # task/scope/freshness validation in ShoppingTurn._assessment.
        if self.recommendation_proposal and self.assessment_id is None:
            self.assessment_id = self.recommendation_proposal.assessment_id
        if self.recommendation_proposal and self.assessment_id and self.recommendation_proposal.assessment_id != self.assessment_id:
            raise ValueError("recommendation_proposal.assessment_id must match assessment_id")
        if self.answer_boundary and (self.kind != "answered" or self.answer_purpose != "evidence"):
            raise ValueError("answer_boundary requires answered evidence")
        keys = [c.key for c in self.claims]
        if len(set(keys)) != len(keys):
            raise ValueError("claim keys must be unique")
        return self


class AssessmentRequest(Contract):
    product_ids: list[str] = Field(min_length=1)
    scope: Literal["formal", "hypothetical"] = "formal"
    purpose: Literal["compare", "recommend"] = "recommend"


class DecisionAction(Contract):
    action: Literal["shortlist_add", "shortlist_remove", "focus_set", "exclude", "restore",
                    "select_tentative", "select_confirmed", "selection_clear", "compare"]
    product_ids: list[str] = Field(default_factory=list)
    task_id: str
    display_id: str
    request_id: str = Field(min_length=1)
