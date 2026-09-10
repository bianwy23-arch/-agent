"""Validated tool contracts. Service-owned IDs cannot be supplied by the model."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    status: Literal["active", "no_preference"]
    strength: Literal["hard", "soft"]
    value: Money | Predicate | str | None


class Operation(Contract):
    target: Literal["requirements", "excluded"]
    key: str
    value: Requirement | bool | None


class Group(Contract):
    group_id: str
    quote: str
    action: Literal["apply", "undo", "clarify", "cancel_clarification", "explore", "end_exploration"]
    operations: list[Operation] = Field(default_factory=list)
    dependent: bool = False
    undo_field: str | None = None
    clarification_fields: list[str] = Field(default_factory=list)
    pending_turn_id: str | None = None
    pending_group_id: str | None = None


class TurnPlan(Contract):
    category: str | None = None
    new_task: bool = False
    resume_task_id: str | None = None
    groups: list[Group] = Field(default_factory=list)
    scope_ids: list[str] | None = None
    stop_requested: bool = False
    cheapest_requested: bool = False

    @model_validator(mode="after")
    def distinct_groups(self):
        if len({g.group_id for g in self.groups}) != len(self.groups):
            raise ValueError("group IDs must be unique within the turn")
        if self.new_task and self.resume_task_id:
            raise ValueError("cannot create and resume a task together")
        return self


class Citation(Contract):
    product_id: str
    field: str
    quote: str


class FinalAnswer(Contract):
    kind: Literal["answered", "recommendation", "needs_user", "no_match", "data_limited", "execution_limited", "stopped"]
    message: str
    product_ids: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    scope: Literal["formal", "hypothetical"] = "formal"
