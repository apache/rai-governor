"""A workload: a deployed agent a site admin publishes for projects (B11).

Committers launch it for a project they belong to, from the Workloads page,
setting only the inputs listed in ``inputs``. ``fixed_inputs`` (tokens,
model profiles, the output repository) are the admin's: they are added to
every run and never sent to anyone but site admins.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

INPUT_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
WORKLOAD_ID_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")


class WorkloadInput(BaseModel):
    """One input a committer sets. ``repository`` inputs only take the
    project's own repositories (decision 4 of the self-serve design)."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    label: str
    kind: Literal["repository", "text", "choice"] = "text"
    default: Optional[str] = None
    choices: List[str] = Field(default_factory=list)
    required: bool = True
    help: Optional[str] = None
    # For text inputs: what a value must match, whole (Python re). Without
    # one, a text value is a plain token: letters, digits and ._/+@:=-, no
    # "..", no "//", no leading or trailing slash.
    pattern: Optional[str] = Field(None, max_length=500)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not INPUT_NAME_RE.match(v):
            raise ValueError("input names are identifiers")
        return v

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"pattern does not compile: {e}")
        return v

    @model_validator(mode="after")
    def _choices(self) -> "WorkloadInput":
        if self.kind == "choice" and not self.choices:
            raise ValueError(f"choice input {self.name!r} needs choices")
        if self.kind == "choice" and self.default is not None and self.default not in self.choices:
            raise ValueError(f"default of {self.name!r} is not one of its choices")
        return self


class WorkloadBody(BaseModel):
    """What a site admin sends to publish or change a workload."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=2000)
    deployment: str = Field(..., min_length=1, max_length=200)
    # Project names, or ["*"] for every project.
    projects: List[str] = Field(default_factory=lambda: ["*"])
    inputs: List[WorkloadInput] = Field(default_factory=list)
    fixed_inputs: Dict[str, Any] = Field(default_factory=dict, alias="fixedInputs")
    # Per project, the repositories a repository input offers. A project not
    # listed gets a free-text field checked against the naming rule.
    repositories: Dict[str, List[str]] = Field(default_factory=dict)
    produces: Optional[str] = Field(None, max_length=500)
    memory_domain_note: Optional[str] = Field(None, alias="memoryNote", max_length=200)
    typical_duration: Optional[str] = Field(None, alias="typicalDuration", max_length=100)
    enabled: bool = True

    @model_validator(mode="after")
    def _no_overlap(self) -> "WorkloadBody":
        names = [i.name for i in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError("input names repeat")
        overlap = set(names) & set(self.fixed_inputs)
        if overlap:
            raise ValueError(f"inputs both set by committers and fixed: {sorted(overlap)}")
        return self


class Workload(WorkloadBody):
    id: str
    published_by: Optional[str] = Field(None, alias="publishedBy")
    published_at: Optional[datetime] = Field(None, alias="publishedAt")
    updated_at: Optional[datetime] = Field(None, alias="updatedAt")


class RunWorkloadBody(BaseModel):
    project: str = Field(..., min_length=1, max_length=64)
    inputs: Dict[str, Any] = Field(default_factory=dict)
