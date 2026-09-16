from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

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


class MessageBundle(BaseModel):
    conversation: ConversationIdentity
    time_range: TimeRange
    messages: list[NormalizedMessage]
    missing_message_ids: list[str] = Field(default_factory=list)


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
