"""Memory entries: the on-disk format shared with apache/tooling-memory.

An entry is Markdown with a YAML frontmatter block:

    ---
    id: mem-scan-k3v9q2xa
    type: fact
    domain: dom-scan
    ...
    ---
    body

Field names, enum values, the id scheme and the fence are the contract in
tooling-memory's ``crates/core/src/lib.rs`` (``Frontmatter``) and
``crates/core/src/id.rs``. Keeping them identical means that project's tools
can read the record gofannon writes, and the reverse. Change a field here only
alongside a change there.
"""

from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import yaml

FENCE = "---"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ID_ALPHABET = string.ascii_lowercase + string.digits


class EntryType(str, Enum):
    DECISION = "decision"
    FACT = "fact"
    CONVENTION = "convention"
    METHOD = "method"
    HANDOFF = "handoff"
    POLICY = "policy"


class TrustTier(str, Enum):
    RAW = "raw"
    CURATED = "curated"
    VERIFIED = "verified"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Label(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class Status(str, Enum):
    DEMOTED = "demoted"
    SUPERSEDED = "superseded"
    ERASED = "erased"


class EntryError(ValueError):
    """An entry that does not parse or does not validate."""


def valid_name(s: str) -> bool:
    """Same rule as tooling-memory's store: safe as one path segment."""
    return bool(_NAME_RE.match(s or ""))


def _domain_tag(domain: str) -> str:
    return domain[4:] if domain.startswith("dom-") else domain


def new_entry_id(domain: str) -> str:
    """``mem-<domain tag>-<8 chars>``: stable, permanent, meaningless."""
    suffix = "".join(secrets.choice(_ID_ALPHABET) for _ in range(8))
    return f"mem-{_domain_tag(domain)}-{suffix}"


def is_entry_id(s: str) -> bool:
    return s.startswith("mem-") and valid_name(s) and s.count("-") >= 2


@dataclass
class Frontmatter:
    id: str
    type: EntryType
    domain: str
    author: str
    created: datetime
    source: List[str] = field(default_factory=list)
    confidence: Optional[Confidence] = None
    trust_tier: TrustTier = TrustTier.RAW
    review_by: Optional[date] = None
    supersedes: Optional[str] = None
    policy_context: List[str] = field(default_factory=list)
    classification: Label = Label.PUBLIC
    status: Optional[Status] = None
    origin: Optional[str] = None
    summary: Optional[str] = None
    title: Optional[str] = None
    # The gofannon run that wrote it, for agent-written entries (B11).
    run: Optional[str] = None

    def validate(self) -> None:
        if not is_entry_id(self.id):
            raise EntryError(f"bad entry id {self.id!r}")
        if not valid_name(self.domain):
            raise EntryError(f"bad domain {self.domain!r}")
        if not self.author:
            raise EntryError("author is required")
        if self.created.tzinfo is None:
            raise EntryError("created must be timezone-aware")
        if self.summary is not None and "\n" in self.summary:
            raise EntryError("summary is one line")

    def to_dict(self) -> Dict[str, Any]:
        """Frontmatter as the YAML mapping, omitting what tooling-memory omits."""
        d: Dict[str, Any] = {
            "id": self.id,
            "type": self.type.value,
            "domain": self.domain,
            "author": self.author,
            "created": self.created.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        if self.source:
            d["source"] = list(self.source)
        if self.confidence is not None:
            d["confidence"] = self.confidence.value
        d["trust_tier"] = self.trust_tier.value
        if self.review_by is not None:
            d["review_by"] = self.review_by.isoformat()
        if self.supersedes is not None:
            d["supersedes"] = self.supersedes
        if self.policy_context:
            d["policy_context"] = list(self.policy_context)
        d["classification"] = self.classification.value
        for key in ("status", "origin", "summary", "title", "run"):
            value = getattr(self, key)
            if value is not None:
                d[key] = value.value if isinstance(value, Enum) else value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Frontmatter":
        try:
            # YAML resolves unquoted timestamps and dates itself; tooling-memory
            # writes them unquoted, gofannon quoted. Accept both.
            created = d["created"]
            if isinstance(created, str):
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if isinstance(created, datetime) and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            review_by = d.get("review_by")
            if isinstance(review_by, datetime):
                review_by = review_by.date()
            elif isinstance(review_by, str):
                review_by = date.fromisoformat(review_by)
            fm = cls(
                id=d["id"],
                type=EntryType(d["type"]),
                domain=d["domain"],
                author=d["author"],
                created=created,
                source=list(d.get("source") or []),
                confidence=Confidence(d["confidence"]) if d.get("confidence") else None,
                trust_tier=TrustTier(d.get("trust_tier", "raw")),
                review_by=review_by,
                supersedes=d.get("supersedes"),
                policy_context=list(d.get("policy_context") or []),
                classification=Label(d.get("classification", "public")),
                status=Status(d["status"]) if d.get("status") else None,
                origin=d.get("origin"),
                summary=d.get("summary"),
                title=d.get("title"),
                run=d.get("run"),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise EntryError(f"invalid frontmatter: {exc}") from exc
        fm.validate()
        return fm


@dataclass
class Entry:
    front: Frontmatter
    body: str

    @property
    def display_title(self) -> str:
        if self.front.title:
            return self.front.title
        for line in self.body.splitlines():
            if line.startswith("#"):
                return line.lstrip("#").strip()
        return self.front.id

    @classmethod
    def parse(cls, text: str) -> "Entry":
        """Parse ``---\\nyaml\\n---\\nbody``."""
        lines = text.split("\n")
        if not lines or lines[0].rstrip("\r") != FENCE:
            raise EntryError("entry does not start with a --- frontmatter block")
        for i in range(1, len(lines)):
            if lines[i].rstrip("\r") == FENCE:
                raw = "\n".join(lines[1:i])
                body = "\n".join(lines[i + 1 :])
                break
        else:
            raise EntryError("frontmatter block is not closed")
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            raise EntryError(f"frontmatter is not YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise EntryError("frontmatter is not a mapping")
        return cls(front=Frontmatter.from_dict(data), body=body)

    def render(self) -> str:
        self.front.validate()
        yaml_text = yaml.safe_dump(
            self.front.to_dict(), sort_keys=False, allow_unicode=True
        )
        return f"{FENCE}\n{yaml_text}{FENCE}\n{self.body}"
