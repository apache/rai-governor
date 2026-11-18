"""Triage: a person's disposition of a scan finding, and the guidance it writes.

Design: "Scans that remember: triage, guidance and prior findings".

A disposition is a review with more to say. It applies the trust action it
implies (valid and by design verify, the rest demote), records the kind and
the reason in the review ledger, and copies them to the finding's index row
(index.ROW_ONLY), so a search can filter on them. The entry's frontmatter is
shared with tooling-memory and is not changed beyond the review itself.

"Needs context" and "by design" may also write a guidance entry, as the person:
ordinary memory entries with an agreed origin.

    guidance:<owner/repo>              every scanner
    guidance:asvs:<owner/repo>         ASVS only
    guidance:glasswing:<owner/repo>    Glasswing only
    proposal:guidance:<owner/repo>     proposed by a scan, waiting in triage
    guidance:digest:<owner/repo>       one digest per repo, drafted by an agent

"Always use" is the entry's type, policy; other guidance is convention
(context) or decision (by design).

Every disposition, one or many, gets a batch id, so the existing undo puts
the finding back and withdraws the guidance it wrote.

Guidance written in triage by someone who reviews the domain is curated
as it is written, with a review record saying so; it needs no second
person.

Which guidance a scan loads (guidance_for): a person's at any tier, an
agent's once someone has curated it. A digest is an agent's, so it loads
once a person has accepted it, which curates it.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional

from fastapi import HTTPException

from .entry import Entry, EntryError, EntryType, Status, TrustTier
from .index import INDEX_DB, MAX_SEEN_RUNS
from .service import (
    REVIEW_INTERVAL_DAYS,
    REVIEWS_DB,
    MemoryService,
    NotFound,
    OwnEntry,
    Refused,
    ReviewAction,
    WriteRequest,
    _CURRENT_RUN,
    _TIER_RANK,
)

USAGE_DB = "memory_usage"

SCANNERS = ("asvs", "glasswing")

# Most guidance text one entry holds: an imported audit_guidance file can
# be long, and a scan already loads such files whole today.
MAX_GUIDANCE_CHARS = 32000
MAX_EVIDENCE_CHARS = 16000


class Kind(str, Enum):
    VALID = "valid"                 # worth fixing
    DUPLICATE = "duplicate"         # of a finding, issue or PR
    FP_WRONG = "fp_wrong"           # false positive: the finding is wrong
    FP_CONTEXT = "fp_context"       # false positive: the scanner lacked context
    BY_DESIGN = "by_design"         # true, and accepted by design or as a risk


# The trust action each kind implies.
ACTION = {
    Kind.VALID: ReviewAction.VERIFY,
    Kind.DUPLICATE: ReviewAction.DEMOTE,
    Kind.FP_WRONG: ReviewAction.DEMOTE,
    Kind.FP_CONTEXT: ReviewAction.DEMOTE,
    Kind.BY_DESIGN: ReviewAction.VERIFY,
}

WRITES_GUIDANCE = (Kind.FP_CONTEXT, Kind.BY_DESIGN)
CURATED_IN_TRIAGE = "Curated on writing: written in triage by a reviewer of this domain"

# The review record's reason when the person gives none: a review that
# changes the tier needs one, and the disposition is the reason.
_DEFAULT_WHY = {
    Kind.VALID: "Triaged as valid",
    Kind.DUPLICATE: "Triaged as a duplicate",
    Kind.FP_WRONG: "Triaged as a false positive: wrong",
    Kind.FP_CONTEXT: "Triaged as a false positive: needs context",
    Kind.BY_DESIGN: "Triaged as by design",
}

_FINDING_ORIGIN = re.compile(r"^(?:asvs|glasswing):finding:(.+)$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def repo_of(origin: Optional[str]) -> Optional[str]:
    """owner/repo from a finding's origin (which may carry a sub-path)."""
    m = _FINDING_ORIGIN.match(origin or "")
    if not m:
        return None
    parts = m.group(1).strip("/").split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else None


def guidance_origin(repo: str, scope: str = "all") -> str:
    if scope in (None, "", "all"):
        return f"guidance:{repo}"
    if scope not in SCANNERS:
        raise Refused(f"scope is all, asvs or glasswing, not {scope!r}")
    return f"guidance:{scope}:{repo}"


def guidance_origins(repo: str, scanner: Optional[str] = None) -> List[str]:
    """What a scanner of this repo loads: all-scanner guidance and its own."""
    out = [f"guidance:{repo}"]
    out += [f"guidance:{s}:{repo}" for s in SCANNERS if scanner in (None, s)]
    return out


def digest_origin(repo: str) -> str:
    return f"guidance:digest:{repo}"


def is_human(author: Optional[str]) -> bool:
    return (author or "").startswith("human:")


def loads(row: Dict[str, Any], min_trust: str = "human") -> bool:
    """The trust rule for guidance a scan loads. ``min_trust`` "human": a
    person's entry at any tier, an agent's once curated. "curated": every
    entry once curated."""
    if row.get("status"):
        return False
    tier = row.get("trust_tier") or "raw"
    if tier in ("curated", "verified"):
        return True
    return min_trust == "human" and is_human(row.get("author"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _batch_id(prefix: str) -> str:
    return f"{prefix}-" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + secrets.token_hex(4)


def _one_line(text: str, n: int = 300) -> str:
    first = next((l.strip() for l in (text or "").splitlines() if l.strip()), "")
    first = first.lstrip("#").strip()
    return first if len(first) <= n else first[: n - 1].rstrip() + "…"


class Triage:
    def __init__(self, service: MemoryService) -> None:
        self.s = service

    # ------------------------------------------------------------------
    # Dispositions
    # ------------------------------------------------------------------

    async def _check(self, entry_id: str, reviewer: str, may_read, may_review) -> Entry:
        entry = await self.s.get(entry_id)
        if may_read is not None and not may_read(entry.front.domain):
            raise NotFound(entry_id)
        if may_review is not None and not may_review(entry.front.domain):
            raise Refused(f"You can read {entry.front.domain} but not decide on it")
        if entry.front.status is not None:
            raise Refused(f"{entry_id} is {entry.front.status.value}; undo the decision first")
        if entry.front.author == reviewer:
            raise OwnEntry("You wrote this entry; someone else has to decide on it")
        return entry

    async def dispose(
        self,
        ids: List[str],
        kind: Kind,
        reviewer: str,
        why: str,
        duplicate_of: Optional[str] = None,
        filed_as: Optional[str] = None,
        guidance: Optional[Dict[str, Any]] = None,
        may_read=None,
        may_review=None,
    ) -> Dict[str, Any]:
        """One disposition over one finding or many, under one batch id.

        Findings that can't take it are skipped and listed. Context and
        by-design guidance, when given, becomes one entry per repo, citing
        every finding it covers. The reason and the guidance are optional:
        the disposition itself is what a later scan is told.
        """
        why = (why or "").strip()
        if kind is Kind.DUPLICATE and not (duplicate_of or "").strip():
            raise Refused("a duplicate says what it duplicates")
        g = None
        text = ((guidance or {}).get("text") or "").strip()
        if kind in WRITES_GUIDANCE and text:
            g = dict(guidance or {})
            g["text"] = text
            if len(g["text"]) > MAX_GUIDANCE_CHARS:
                raise Refused(f"guidance is at most {MAX_GUIDANCE_CHARS} characters")
            guidance_origin("x/y", g.get("scope") or "all")   # check the scope early
        batch = _batch_id("d")

        ok: List[Entry] = []
        skipped = []
        for entry_id in dict.fromkeys(ids):
            try:
                ok.append(await self._check(entry_id, reviewer, may_read, may_review))
            except NotFound:
                skipped.append({"id": entry_id, "reason": "No such entry"})
            except OwnEntry:
                skipped.append({"id": entry_id, "reason": "You wrote it; someone else decides on it"})
            except (Refused, EntryError) as exc:
                skipped.append({"id": entry_id, "reason": str(exc)})

        # Guidance first, so each finding's record can name it. One entry per
        # (domain, repo): a decision about one repo is not another's.
        guidance_by: Dict[tuple, str] = {}
        if g is not None and ok:
            groups: Dict[tuple, List[Entry]] = {}
            for e in ok:
                repo = g.get("repo") or repo_of(e.front.origin)
                if not repo or not _REPO.match(repo):
                    skipped.append({"id": e.front.id, "reason":
                                    "Can't tell which repository this finding is in; give the repo"})
                    continue
                groups.setdefault((e.front.domain, repo), []).append(e)
            ok = [e for grp in groups.values() for e in grp]
            for (domain, repo), grp in groups.items():
                gid = await self._write_guidance(domain, repo, kind, g, reviewer,
                                                 source=[e.front.id for e in grp],
                                                 may_review=may_review)
                guidance_by[(domain, repo)] = gid

        applied, guidance_ids = [], sorted(set(guidance_by.values()))
        for e in ok:
            repo = (g or {}).get("repo") or repo_of(e.front.origin)
            gid = guidance_by.get((e.front.domain, repo))
            decision = {"kind": kind.value, "why": why, "by": reviewer, "at": _now(), "batch": batch}
            if duplicate_of:
                decision["duplicate_of"] = duplicate_of.strip()
            if filed_as:
                decision["filed_as"] = filed_as.strip()
            if gid:
                decision["guidance_id"] = gid
            action = ACTION[kind]
            target = {ReviewAction.VERIFY: TrustTier.VERIFIED}.get(action)
            if target is not None and _TIER_RANK[e.front.trust_tier] >= _TIER_RANK[target]:
                action = ReviewAction.KEEP           # verified already: the review date moves on
            try:
                await self.s.review(
                    e.front.id, action, reviewer, why or _DEFAULT_WHY[kind], batch=batch,
                    extra={"disposition": {k: v for k, v in decision.items()
                                           if k not in ("by", "at", "batch", "why")}},
                    row={"disposition": kind.value, "decision": decision},
                )
                applied.append(e.front.id)
            except (Refused, OwnEntry, NotFound, EntryError) as exc:
                skipped.append({"id": e.front.id, "reason": str(exc) or type(exc).__name__})
        # Guidance whose findings all fell through has nothing behind it.
        for key, gid in guidance_by.items():
            if not any((e.front.domain, (g or {}).get("repo") or repo_of(e.front.origin)) == key
                       and e.front.id in applied for e in ok):
                await self.s._withdraw(gid, reviewer, "no finding took the disposition", batch)
                guidance_ids.remove(gid)
        return {"batch": batch, "kind": kind.value, "applied": applied, "skipped": skipped,
                "guidance": guidance_ids}

    async def _curate_if_reviewer(self, entry: Entry, author: str, may_review) -> None:
        """Guidance written in triage by someone who reviews its domain (a
        site admin, or anyone with review rights there) is curated as it is
        written; anyone else's waits for review at raw."""
        if may_review is not None and may_review(entry.front.domain):
            await self.s.curate_written(entry, author, CURATED_IN_TRIAGE)

    async def _write_guidance(self, domain: str, repo: str, kind: Kind, g: Dict[str, Any],
                              author: str, source: List[str], may_review=None) -> str:
        etype = EntryType.POLICY if g.get("always") else (
            EntryType.DECISION if kind is Kind.BY_DESIGN else EntryType.CONVENTION)
        text = g["text"]
        req = WriteRequest(
            domain=domain, type=etype, body=text.rstrip() + "\n",
            summary=(g.get("summary") or _one_line(text)).strip()[:300] or "Guidance",
            title=(g.get("title") or None),
            source=list(source) + [s for s in (g.get("source") or []) if isinstance(s, str)],
            review_by=datetime.now(timezone.utc).date() + timedelta(days=REVIEW_INTERVAL_DAYS),
            origin=guidance_origin(repo, g.get("scope") or "all"),
        )
        entry = await self.s.write(req, author=author)
        await self._curate_if_reviewer(entry, author, may_review)
        return entry.front.id

    # ------------------------------------------------------------------
    # Proposals: guidance a scan suggests, waiting for a person
    # ------------------------------------------------------------------

    async def accept_proposals(self, ids: List[str], reviewer: str, why: str = "",
                               edits: Optional[Dict[str, Dict[str, Any]]] = None,
                               may_read=None, may_review=None) -> Dict[str, Any]:
        """Each proposal accepted becomes guidance written by the person, which
        supersedes the proposal: the agent that proposed it never vouches for
        itself. ``edits`` maps a proposal id to {text, summary, scope, always}."""
        batch = _batch_id("p")
        accepted, skipped = [], []
        for pid in dict.fromkeys(ids):
            try:
                prop = await self._check(pid, reviewer, may_read, may_review)
                m = re.match(r"^proposal:guidance:(.+)$", prop.front.origin or "")
                if not m:
                    raise Refused("not a guidance proposal")
                repo = m.group(1)
                ed = (edits or {}).get(pid) or {}
                text = (ed.get("text") or prop.body).strip()
                if not text or len(text) > MAX_GUIDANCE_CHARS:
                    raise Refused(f"guidance is 1 to {MAX_GUIDANCE_CHARS} characters")
                etype = EntryType.POLICY if ed.get("always") else (
                    prop.front.type if prop.front.type in (EntryType.DECISION, EntryType.CONVENTION)
                    else EntryType.CONVENTION)
                req = WriteRequest(
                    domain=prop.front.domain, type=etype, body=text.rstrip() + "\n",
                    summary=(ed.get("summary") or (prop.front.summary if not ed.get("text") else "")
                             or _one_line(text)).strip()[:300],
                    title=ed.get("title") or prop.front.title,
                    source=[pid] + list(prop.front.source),
                    review_by=datetime.now(timezone.utc).date() + timedelta(days=REVIEW_INTERVAL_DAYS),
                    supersedes=pid,
                    origin=guidance_origin(repo, ed.get("scope") or "all"),
                )
                new = await self.s.write(req, author=reviewer)
                await self._curate_if_reviewer(new, reviewer, may_review)
                await self._ledger(prop, "accept", reviewer, why or "accepted in triage", batch,
                                   {"guidance_id": new.front.id})
                accepted.append({"proposal": pid, "guidance": new.front.id})
            except NotFound:
                skipped.append({"id": pid, "reason": "No such entry"})
            except OwnEntry:
                skipped.append({"id": pid, "reason": "You wrote it; someone else decides on it"})
            except (Refused, EntryError) as exc:
                skipped.append({"id": pid, "reason": str(exc)})
        return {"batch": batch, "accepted": accepted, "skipped": skipped}

    async def reject_proposals(self, ids: List[str], reviewer: str, why: str,
                               may_read=None, may_review=None) -> Dict[str, Any]:
        """A rejected proposal is demoted with the reason, so it doesn't come back."""
        why = (why or "").strip()
        if not why:
            raise Refused("say why the proposal is wrong: a later scan's proposals are checked against it")
        batch = _batch_id("p")
        rejected, skipped = [], []
        for pid in dict.fromkeys(ids):
            try:
                prop = await self._check(pid, reviewer, may_read, may_review)
                if not (prop.front.origin or "").startswith("proposal:guidance:"):
                    raise Refused("not a guidance proposal")
                await self.s.review(pid, ReviewAction.DEMOTE, reviewer, why, batch=batch,
                                    extra={"proposal": "reject"})
                rejected.append(pid)
            except NotFound:
                skipped.append({"id": pid, "reason": "No such entry"})
            except OwnEntry:
                skipped.append({"id": pid, "reason": "You wrote it; someone else decides on it"})
            except (Refused, EntryError) as exc:
                skipped.append({"id": pid, "reason": str(exc)})
        return {"batch": batch, "rejected": rejected, "skipped": skipped}

    async def _ledger(self, entry: Entry, action: str, by: str, why: str, batch: str,
                      extra: Optional[Dict[str, Any]] = None) -> None:
        """A review record for something that changes no tier (an accepted
        proposal is superseded by write())."""
        now = datetime.now(timezone.utc)
        front = entry.front
        record = {"domain": front.domain, "entry": front.id, "action": action,
                  "tier": front.trust_tier.value, "by": by, "author": front.author,
                  "why": why, "classification": front.classification.value,
                  "at": now.isoformat().replace("+00:00", "Z"), "batch": batch, **(extra or {})}
        await asyncio.to_thread(self.s.index.db.save, REVIEWS_DB,
                                f"{front.id}:{now.strftime('%Y%m%dT%H%M%S.%fZ')}", record)

    # ------------------------------------------------------------------
    # What scans load
    # ------------------------------------------------------------------

    def _rows(self, domains: Iterable[str], origins: List[str], include_inactive=False):
        out = []
        for d in domains:
            out += self.s.index.search(d, None, None, None, include_inactive, 1000,
                                       origins=origins)
        return out

    async def guidance_for(self, domains: List[str], repo: str, scanner: Optional[str] = None,
                           min_trust: str = "human", bodies: bool = True) -> Dict[str, Any]:
        """The guidance a scan of ``repo`` by ``scanner`` loads, Always use
        first, then newest; and the accepted digest, if any."""
        if min_trust not in ("human", "curated"):
            raise Refused("min_trust is human or curated")
        rows = await asyncio.to_thread(self._rows, domains, guidance_origins(repo, scanner))
        rows = [r for r in rows if loads(r, min_trust)]
        rows.sort(key=lambda r: r.get("created") or "", reverse=True)
        rows.sort(key=lambda r: r.get("type") != "policy")
        entries = []
        for r in rows:
            item = {"id": r["id"], "domain": r["domain"], "type": r.get("type"),
                    "always": r.get("type") == "policy", "origin": r.get("origin"),
                    "scope": (r.get("origin") or "").split(":")[1] if (r.get("origin") or "").count(":") >= 2 else "all",
                    "title": r.get("title"), "summary": r.get("summary"),
                    "author": r.get("author"), "trust_tier": r.get("trust_tier"),
                    "source": r.get("source") or [], "created": r.get("created"),
                    "review_by": r.get("review_by")}
            if bodies:
                try:
                    item["body"] = (await self.s.get(r["id"])).body
                except NotFound:
                    continue
                item["chars"] = len(item["body"])
            entries.append(item)
        digest = await self.current_digest(domains, repo, bodies=bodies)
        return {"repo": repo, "scanner": scanner, "min_trust": min_trust,
                "entries": entries, "digest": digest}

    async def current_digest(self, domains: List[str], repo: str, bodies: bool = True):
        rows = await asyncio.to_thread(self._rows, domains, [digest_origin(repo)])
        rows = [r for r in rows if loads(r, "curated")]
        if not rows:
            return None
        r = max(rows, key=lambda r: r.get("created") or "")
        out = {"id": r["id"], "domain": r["domain"], "created": r.get("created"),
               "summary": r.get("summary"), "trust_tier": r.get("trust_tier")}
        if bodies:
            out["body"] = (await self.s.get(r["id"])).body
        return out

    # ------------------------------------------------------------------
    # The digest: an agent drafts it, a person accepts each version
    # ------------------------------------------------------------------

    async def digest_view(self, domain: str, repo: str) -> Dict[str, Any]:
        """The accepted digest, the newest draft after it, and what the draft
        changes, rule by rule (a rule is a "- " line)."""
        current = await self.current_digest([domain], repo)
        rows = await asyncio.to_thread(self._rows, [domain], [digest_origin(repo)])
        drafts = [r for r in rows if (r.get("trust_tier") or "raw") == "raw"
                  and (not current or (r.get("created") or "") > (current.get("created") or ""))]
        draft = None
        if drafts:
            r = max(drafts, key=lambda r: r.get("created") or "")
            draft = {"id": r["id"], "created": r.get("created"), "summary": r.get("summary"),
                     "author": r.get("author"), "body": (await self.s.get(r["id"])).body}
        diff = None
        if draft:
            old = _rules(current["body"]) if current else []
            new = _rules(draft["body"])
            diff = {"added": [x for x in new if x not in old],
                    "removed": [x for x in old if x not in new],
                    "kept": [x for x in new if x in old]}
        return {"repo": repo, "current": current, "draft": draft, "diff": diff,
                "pending": await self.decisions_since(domain, repo, current)}

    async def decisions_since(self, domain: str, repo: str, current) -> int:
        """Guidance and dispositions newer than the accepted digest: what a
        rebuild would add."""
        since = (current or {}).get("created") or ""
        rows = await asyncio.to_thread(self._rows, [domain], guidance_origins(repo), True)
        n = sum(1 for r in rows if (r.get("created") or "") > since and loads(r))
        found = await asyncio.to_thread(self.s.index.search, domain, None, None, None, True, 1000,
                                        False, False, None, "any")
        n += sum(1 for r in found
                 if repo_of(r.get("origin")) == repo
                 and ((r.get("decision") or {}).get("at") or "") > since)
        return n

    async def accept_digest(self, entry_id: str, reviewer: str, why: str = "",
                            may_read=None, may_review=None) -> Dict[str, Any]:
        """Accepting a draft curates it, which is what lets scans load it, and
        supersedes the version it replaces."""
        entry = await self._check(entry_id, reviewer, may_read, may_review)
        m = re.match(r"^guidance:digest:(.+)$", entry.front.origin or "")
        if not m:
            raise Refused("not a guidance digest")
        if entry.front.trust_tier is not TrustTier.RAW:
            raise Refused("this version is accepted already")
        repo = m.group(1)
        current = await self.current_digest([entry.front.domain], repo, bodies=False)
        batch = _batch_id("g")
        before_text, before_front = entry.render(), entry.front.to_dict()
        entry.front.trust_tier = TrustTier.CURATED
        if current and current["id"] != entry_id:
            entry.front.supersedes = current["id"]
        entry.front.review_by = datetime.now(timezone.utc).date() + timedelta(days=REVIEW_INTERVAL_DAYS)
        await self.s._record(entry, "curate", reviewer, why or "accepted the digest in triage",
                             before_front, before_text, batch)
        if current and current["id"] != entry_id:
            old = await self.s.get(current["id"])
            ot, of = old.render(), old.front.to_dict()
            old.front.status = Status.SUPERSEDED
            await self.s._record(old, "supersede", reviewer, f"replaced by {entry_id}", of, ot, batch)
        return {"batch": batch, "accepted": entry_id,
                "superseded": current["id"] if current and current["id"] != entry_id else None}

    # ------------------------------------------------------------------
    # Evidence and use, written by scans
    # ------------------------------------------------------------------

    async def annotate(self, entry_id: str, evidence: Dict[str, Any]) -> None:
        """What a scan found about a finding (matches, the patch step's
        verdict, a suggested disposition), on its index row for triage."""
        import json
        if not isinstance(evidence, dict):
            raise ValueError("evidence is a JSON object")
        if len(json.dumps(evidence, default=str)) > MAX_EVIDENCE_CHARS:
            raise ValueError(f"evidence is at most {MAX_EVIDENCE_CHARS} characters of JSON")
        entry = await self.s.get(entry_id)
        extra: Dict[str, Any] = {"evidence": evidence}
        # A later run that found this finding again: its triage shows it.
        run = _CURRENT_RUN.get()
        if run and run != entry.front.run:
            row = await asyncio.to_thread(self.s.index.get, entry_id) or {}
            runs = [r for r in (row.get("seen_in_runs") or []) if r != run] + [run]
            extra["seen_in_runs"] = runs[-MAX_SEEN_RUNS:]
        await asyncio.to_thread(self.s.index.upsert, entry, extra)

    async def used(self, entry_ids: List[str], run: Optional[str], how: str, agent: str) -> int:
        """Record that a run loaded or cited guidance. One record per entry,
        run and how, so a retried step counts once."""
        if how not in ("loaded", "cited"):
            raise ValueError("how is loaded or cited")
        n = 0
        for eid in dict.fromkeys(entry_ids):
            doc_id = f"{eid}:{run or 'none'}:{how}"
            try:
                await asyncio.to_thread(self.s.index.db.save, USAGE_DB, doc_id,
                                        {"entry": eid, "run": run, "how": how, "agent": agent,
                                         "at": _now()})
                n += 1
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
        return n

    async def usage(self, entry_ids: List[str]) -> Dict[str, Dict[str, int]]:
        """Runs that loaded and that cited each entry."""
        want = set(entry_ids)
        rows = await asyncio.to_thread(self.s.index.db.list_all, USAGE_DB)
        out: Dict[str, Dict[str, set]] = {}
        for r in rows:
            if r.get("entry") in want:
                out.setdefault(r["entry"], {"loaded": set(), "cited": set()})[r.get("how") or "loaded"].add(r.get("run"))
        return {e: {k: len(v) for k, v in d.items()} for e, d in out.items()}

    async def guidance_repos(self, domain: str) -> List[str]:
        rows = await asyncio.to_thread(self.s.index.db.find, INDEX_DB, {"domain": domain}, None, None)
        repos = set()
        for r in rows:
            o = r.get("origin") or ""
            if o.startswith(("guidance:", "proposal:guidance:")):
                tail = o.split(":")[-1]
                if _REPO.match(tail):
                    repos.add(tail)
        return sorted(repos)


def _rules(body: str) -> List[str]:
    return [l.strip() for l in (body or "").splitlines() if l.strip().startswith("- ")]
