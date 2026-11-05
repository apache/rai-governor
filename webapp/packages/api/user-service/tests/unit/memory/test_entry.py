from datetime import date, datetime, timezone

import pytest

from services.memory.entry import (
    Entry,
    EntryError,
    EntryType,
    Frontmatter,
    Label,
    TrustTier,
    is_entry_id,
    new_entry_id,
)

# Verbatim from apache/tooling-memory crates/core/src/lib.rs tests: what that
# project writes must parse here.
TOOLING_MEMORY_HANDOFF = (
    "---\nid: mem-x-1\ntype: handoff\ndomain: dom-x\nauthor: human:local:me\n"
    "created: 2026-10-01T10:00:00Z\n---\n\nStopped at step 3.\n"
)


def _entry(**over):
    front = Frontmatter(
        id=over.pop("id", "mem-scan-abcd1234"),
        type=over.pop("type", EntryType.FACT),
        domain=over.pop("domain", "dom-scan"),
        author=over.pop("author", "human:gofannon:alice"),
        created=over.pop("created", datetime(2026, 11, 5, 9, 30, tzinfo=timezone.utc)),
        **over,
    )
    return Entry(front=front, body="# Finding\n\nSQL built by string concatenation.\n")


def test_reads_what_tooling_memory_writes():
    e = Entry.parse(TOOLING_MEMORY_HANDOFF)
    assert e.front.id == "mem-x-1"
    assert e.front.type is EntryType.HANDOFF
    assert e.front.created == datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    assert e.front.trust_tier is TrustTier.RAW
    assert e.front.classification is Label.PUBLIC
    assert e.body == "\nStopped at step 3.\n"


def test_round_trip_keeps_every_field():
    e = _entry(
        source=["https://github.com/apache/example/blob/main/a.py#L10"],
        review_by=date(2027, 2, 1),
        summary="String-built SQL in a.py",
        origin="asvs-scan:run-42",
    )
    again = Entry.parse(e.render())
    assert again.front == e.front
    assert again.body == e.body


def test_render_omits_unset_optionals_like_tooling_memory():
    text = _entry().render()
    for key in ("confidence:", "review_by:", "supersedes:", "status:", "origin:"):
        assert key not in text
    assert text.startswith("---\nid: mem-scan-abcd1234\ntype: fact\n")


def test_ids_have_the_shared_shape():
    i = new_entry_id("dom-scan")
    assert i.startswith("mem-scan-") and len(i) == len("mem-scan-") + 8
    assert is_entry_id(i)
    assert not is_entry_id("../etc/passwd")


@pytest.mark.parametrize(
    "text",
    [
        "no fence\n",
        "---\nid: mem-a-1\n",
        "---\n- a list\n---\n",
        "---\nid: mem-a-1\ntype: rumour\ndomain: dom-a\nauthor: x\ncreated: 2026-10-01T10:00:00Z\n---\n",
        "---\nid: ../x\ntype: fact\ndomain: dom-a\nauthor: x\ncreated: 2026-10-01T10:00:00Z\n---\n",
    ],
)
def test_rejects_malformed(text):
    with pytest.raises(EntryError):
        Entry.parse(text)


def test_title_falls_back_to_first_heading():
    assert _entry().display_title == "Finding"
    assert _entry(title="Explicit").display_title == "Explicit"
