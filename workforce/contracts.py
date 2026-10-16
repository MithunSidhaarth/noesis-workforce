"""Shared typed contracts: bots, tasks, messages, policy decisions, approvals,
external-action results. Everything crossing a component boundary is validated
against one of these models."""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BOT_ID_PATTERN = r"^[a-z][a-z0-9_]{1,40}$"


class MessageType(str, Enum):
    TASK_REQUEST = "TASK_REQUEST"
    TASK_ACCEPTED = "TASK_ACCEPTED"
    TASK_REJECTED = "TASK_REJECTED"
    TASK_PROGRESS = "TASK_PROGRESS"
    TASK_RESULT = "TASK_RESULT"
    INFORMATION_REQUEST = "INFORMATION_REQUEST"
    INFORMATION_RESPONSE = "INFORMATION_RESPONSE"
    RESEARCH_FINDING = "RESEARCH_FINDING"
    VERIFICATION_REQUEST = "VERIFICATION_REQUEST"
    VERIFICATION_RESULT = "VERIFICATION_RESULT"
    APPROVAL_REQUEST = "APPROVAL_REQUEST"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    BLOCKER = "BLOCKER"
    RETRY_REQUEST = "RETRY_REQUEST"
    CANCEL_REQUEST = "CANCEL_REQUEST"
    TASK_COMPLETED = "TASK_COMPLETED"
    ALERT = "ALERT"


class TaskStatus(str, Enum):
    pending = "pending"            # waiting on dependencies
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"              # will retry (transient state between attempts)
    dead = "dead"                  # retries exhausted -> dead letter
    cancelled = "cancelled"
    deferred = "deferred"          # budget/pause deferral, will be re-queued
    waiting_approval = "waiting_approval"


TERMINAL_TASK_STATES = {TaskStatus.succeeded.value, TaskStatus.dead.value, TaskStatus.cancelled.value}


class RetryPolicy(BaseModel):
    max_attempts: int = Field(3, ge=1, le=10)
    base_delay_s: float = Field(10, ge=0.0)
    max_delay_s: float = Field(3600, gt=0)


class ScheduleSpec(BaseModel):
    name: str
    kind: str
    every: str | None = None          # e.g. "30m", "6h", "1d"
    at: str | None = None             # "HH:MM" local time
    weekdays: str | None = None       # "mon,wed,fri"
    payload: dict[str, Any] = Field(default_factory=dict)
    allow_overlap: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def _one_of(self):
        if bool(self.every) == bool(self.at):
            raise ValueError("schedule needs exactly one of 'every' or 'at'")
        return self


class ModelPreference(BaseModel):
    default_capability: str = "classify"
    allowed_tiers: list[Literal["none", "cheap", "standard", "premium"]] = Field(default_factory=lambda: ["none", "cheap"])
    daily_budget_usd: float = Field(0.0, ge=0)
    daily_call_limit: int = Field(50, ge=0)


class BotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=BOT_ID_PATTERN)
    name: str
    purpose: str
    agent: str                                # python class key in the agent registry
    instructions_file: str | None = None
    enabled: bool = True
    tools: list[str] = Field(default_factory=list)          # allowed external action names
    can_message: list[str] = Field(default_factory=list)    # bot ids it may message ('*' = any)
    publish_topics: list[str] = Field(default_factory=list)
    subscribe_topics: list[str] = Field(default_factory=list)
    memory_scope: str | None = None                         # defaults to own id
    knowledge_access: list[str] = Field(default_factory=lambda: ["read"])
    model: ModelPreference = Field(default_factory=ModelPreference)
    schedules: list[ScheduleSpec] = Field(default_factory=list)
    max_concurrency: int = Field(1, ge=1, le=16)
    task_timeout_s: float = Field(300, gt=0, le=6 * 3600)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    max_delegation_depth: int = Field(4, ge=0, le=8)
    settings: dict[str, Any] = Field(default_factory=dict)

    @property
    def scope(self) -> str:
        return self.memory_scope or self.id


class TaskSpec(BaseModel):
    """A request to enqueue work for a bot."""
    bot_id: str = Field(pattern=BOT_ID_PATTERN)
    kind: str = Field(min_length=1, max_length=80)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(5, ge=0, le=9)
    workflow_id: str | None = None
    parent_task_id: str | None = None
    correlation_id: str | None = None
    requested_by: str = "system"
    delegation_depth: int = Field(0, ge=0)
    depends_on: list[str] = Field(default_factory=list)
    idempotency_key: str | None = Field(None, max_length=300)
    max_attempts: int | None = None
    timeout_s: float | None = None
    run_after: float | None = None
    deadline: float | None = None


class Message(BaseModel):
    """Inter-agent message. Bots treat payloads as untrusted data."""
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    sender: str = Field(pattern=BOT_ID_PATTERN + r"|^(user|system|scheduler|telegram|dashboard)$")
    recipient: str | None = Field(None, pattern=BOT_ID_PATTERN)
    topic: str | None = Field(None, pattern=r"^[a-z][a-z0-9_.]{1,60}$")
    type: MessageType
    workflow_id: str | None = None
    correlation_id: str | None = None
    parent_task_id: str | None = None
    in_reply_to: str | None = None
    priority: int = Field(5, ge=0, le=9)
    payload: dict[str, Any] = Field(default_factory=dict)
    expires_at: float | None = None
    response_type: MessageType | None = None
    hop_count: int = Field(0, ge=0)
    dedupe_key: str | None = Field(None, max_length=300)
    auth: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    attempts: int = 0
    created_at: float | None = None

    @model_validator(mode="after")
    def _addressed(self):
        if not self.recipient and not self.topic:
            raise ValueError("message needs a recipient or a topic")
        return self

    @field_validator("payload")
    @classmethod
    def _payload_size(cls, v: dict) -> dict:
        import json
        if len(json.dumps(v, default=str)) > 256_000:
            raise ValueError("payload too large (256KB max)")
        return v


class Decision(str, Enum):
    allow = "allow"
    deny = "deny"
    require_approval = "require_approval"


class PolicyDecision(BaseModel):
    decision: Decision
    reasons: list[str] = Field(default_factory=list)
    dry_run: bool = False
    policy: str = ""
    retry_at: float | None = None     # set when the block clears on its own (pause lifted, daily cap resets)

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.allow


class ActionRequest(BaseModel):
    """An external side effect a bot wants to perform."""
    bot_id: str
    action: str                       # e.g. "telegram.send", "email.send"
    target: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    task_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)  # facts used by policy (category, verified...)
    summary: str = ""


class ExternalActionResult(BaseModel):
    id: str
    idempotency_key: str
    action: str
    status: Literal["succeeded", "failed", "dry_run", "blocked", "approval_required", "duplicate", "unknown"]
    dry_run: bool = False
    evidence: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    approval_id: str | None = None
    reasons: list[str] = Field(default_factory=list)
    retry_at: float | None = None


class Approval(BaseModel):
    id: str
    bot_id: str
    action: str
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    task_id: str | None = None
    status: Literal["pending", "granted", "denied", "expired"] = "pending"
