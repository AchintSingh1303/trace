from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .services.agents import REQ_TYPES, TEST_TYPES


class LoginBody(BaseModel):
    email: str
    password: str


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    app_type: str = "web"


class CycleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


def _clean_criteria(values: list[str]) -> list[str]:
    return [v.strip() for v in values if v and v.strip()]


class RequirementCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    req_type: str
    acceptance_criteria: list[str] = []
    code: str | None = Field(default=None, max_length=30)

    @field_validator("req_type")
    @classmethod
    def _type(cls, value: str) -> str:
        if value not in REQ_TYPES:
            raise ValueError(f"req_type must be one of {REQ_TYPES}")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def _criteria(cls, value: list[str]) -> list[str]:
        return _clean_criteria(value)


class RequirementUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    req_type: str | None = None
    acceptance_criteria: list[str] | None = None

    @field_validator("req_type")
    @classmethod
    def _type(cls, value: str | None) -> str | None:
        if value is not None and value not in REQ_TYPES:
            raise ValueError(f"req_type must be one of {REQ_TYPES}")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def _criteria(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_criteria(value)


class GenerateBody(BaseModel):
    selected_test_types: list[str] = Field(default_factory=lambda: list(TEST_TYPES))
    mode: Literal["full", "incremental"] = "full"
    max_tcs: int | None = Field(default=None, ge=1, le=2000)
    # Generator choice. Leave unset to use the server default (the offline rule engine).
    provider: Literal["rules", "anthropic", "openai"] | None = None
    api_key: str | None = Field(default=None, max_length=400, repr=False)
    model: str | None = Field(default=None, max_length=100)
    # Strict grounding also drops cases that mention endpoints or fields the requirement never names.
    strict_grounding: bool = False

    @field_validator("selected_test_types")
    @classmethod
    def _types(cls, value: list[str]) -> list[str]:
        unknown = [v for v in value if v not in TEST_TYPES]
        if unknown or not value:
            raise ValueError(f"selected_test_types must be a non-empty subset of {TEST_TYPES}")
        return list(dict.fromkeys(value))


class LLMCheckBody(BaseModel):
    provider: Literal["anthropic", "openai"]
    api_key: str = Field(min_length=1, max_length=400, repr=False)
    model: str | None = Field(default=None, max_length=100)


class ReviewBody(BaseModel):
    status: Literal["approved", "rejected", "pending"]
    review_notes: str | None = None


class BulkReviewBody(ReviewBody):
    tc_ids: list[int] = Field(min_length=1)


class CaseEdit(BaseModel):
    """A reviewer's correction to a generated case."""

    title: str | None = Field(default=None, min_length=1, max_length=500)
    priority: Literal["critical", "high", "medium", "low"] | None = None
    preconditions: list[str] | None = None
    steps: list[dict] | None = None

    @field_validator("steps")
    @classmethod
    def _steps(cls, value):
        if value is None:
            return None
        clean = [
            {"action": str(s.get("action", "")).strip(), "expected": str(s.get("expected", "")).strip()}
            for s in value
            if str(s.get("action", "")).strip()
        ]
        if not clean:
            raise ValueError("A test case needs at least one step")
        return clean


class KnowledgeCreate(BaseModel):
    kind: Literal["glossary", "rule", "avoid"]
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=2000)


class ImportBody(BaseModel):
    csv: str = Field(min_length=1, max_length=2_000_000)


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    name: str = Field(min_length=1, max_length=120)
    role: Literal["viewer", "tester", "qa_lead", "admin"]
    password: str = Field(min_length=8, max_length=200, repr=False)


class UserUpdate(BaseModel):
    role: Literal["viewer", "tester", "qa_lead", "admin"] | None = None
    password: str | None = Field(default=None, min_length=8, max_length=200, repr=False)
