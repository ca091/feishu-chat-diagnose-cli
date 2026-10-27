from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ConversationType = Literal["group", "p2p"]
CaseStage = Literal[
    "created",
    "resolving_conversation",
    "fetching_messages",
    "assembling_context",
    "awaiting_confirmation",
    "preparing_attachments",
    "diagnosing_repository",
    "completed",
    "failed",
    "cancelled",
]


class TimeRange(BaseModel):
    start: datetime
    end: datetime


class RepositorySettings(BaseModel):
    path: Path
    description: str = ""
    diagnosis_skill: str = ""
    allowed_skills: list[str] = Field(default_factory=list)


class DiagnosisSkill(BaseModel):
    name: str
    path: Path


class ConversationCandidate(BaseModel):
    chat_id: str = ""
    name: str
    conversation_type: ConversationType
    participant_open_id: str = ""
    detail: str = ""


class ConversationIdentity(ConversationCandidate):
    pass


class AttachmentRef(BaseModel):
    source_message_id: str
    file_key: str
    resource_type: Literal["image", "file"]
    media_type: str
    name: str
    kind: Literal["image", "video", "text", "unsupported"]
    status: Literal["available", "inaccessible", "download_failed"] = "available"


class Sender(BaseModel):
    open_id: str
    name: str


class NormalizedMessage(BaseModel):
    message_id: str
    sender: Sender
    created_at: datetime
    message_position: int | None = None
    text: str = ""
    reply_to_message_id: str = ""
    thread_root_id: str = ""
    mentions: list[str] = Field(default_factory=list)
    attachments: list[AttachmentRef] = Field(default_factory=list)


class FetchMetadata(BaseModel):
    missing_message_ids: list[str]


@dataclass(frozen=True)
class FetchResult:
    messages: list[NormalizedMessage]
    missing_message_ids: list[str] = field(default_factory=list)


class MessageBundle(BaseModel):
    conversation: ConversationIdentity
    time_range: TimeRange
    messages: list[NormalizedMessage]
    missing_message_ids: list[str] = Field(default_factory=list)
    analysis_kind: Literal["bug", "code"] = "bug"
    # Legacy cases have no triage scope and analyze all retained messages.
    analysis_message_ids: list[str] | None = None


class DiscussionTopic(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["bug", "requirement", "design", "other", "unknown"]
    summary: str = Field(min_length=1)
    project_related: Literal["yes", "no", "unknown"]
    needs_code: bool = Field(strict=True)
    reason: str = Field(min_length=1)
    message_ids: list[str] = Field(min_length=1)
    missing_information: list[str]

    @model_validator(mode="after")
    def validate_code_decision(self) -> DiscussionTopic:
        if self.needs_code and (self.project_related != "yes" or self.kind == "unknown"):
            raise ValueError("Code analysis requires a known, project-related topic")
        return self


class DiscussionTriage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topics: list[DiscussionTopic] = Field(min_length=1)

    @property
    def code_message_ids(self) -> set[str]:
        return {message_id for topic in self.topics if topic.needs_code for message_id in topic.message_ids}


class SkillAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    applicable: bool = Field(strict=True)
    reason: str = Field(min_length=1)


class TriageCheckpoint(BaseModel):
    triage: DiscussionTriage
    requested_skill: str
    skill_assessment: SkillAssessment | None = None


class VideoObservation(BaseModel):
    timestamp: str
    observation: str


class VideoToolResult(BaseModel):
    source_message_id: str
    summary: str
    observations: list[VideoObservation]
    missing_information: list[str]


class CodeEvidence(BaseModel):
    path: str
    symbol: str = ""
    line: int | None = None
    explanation: str


class Diagnosis(BaseModel):
    problem_summary: str
    facts: list[str]
    root_cause: str
    confidence: int = Field(ge=0, le=100)
    code_evidence: list[CodeEvidence]
    alternative_hypotheses: list[str]
    missing_information: list[str]
    recommended_fix_direction: list[str]
    recommended_verification: list[str]
    skills_used: list[str] = Field(default_factory=list)


class CaseState(BaseModel):
    case_id: str
    stage: CaseStage = "created"
    last_successful_stage: CaseStage | None = None
    failed_stage: CaseStage | None = None
    created_at: datetime
    updated_at: datetime
    error: str = ""
