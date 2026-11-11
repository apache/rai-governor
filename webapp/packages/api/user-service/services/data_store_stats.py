"""Per-namespace totals for the data store: record count, size, the agents
that wrote it, last update (ISSUE-034).

Listing namespaces once read every record's value to total sizes; then
every record's small fields, which on a 400,000-record store still built
400,000 dicts per request. CouchDB keeps the totals in two views instead
(services/database_service/couchdb.py); other backends total the small
fields here.
"""

from typing import Any, Dict, Iterable, Optional

DATA_STORE_DB = "agent_data_store"

# What totalling reads of each record: never the value.
STATS_FIELDS = ["namespace", "sizeBytes", "createdByAgent", "lastAccessedByAgent", "updatedAt"]


def totals_from_docs(docs: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """{namespace: {recordCount, sizeBytes, agents, updatedAt}} from record
    docs (their STATS_FIELDS). A record from before sizes were stored counts
    as its value's size if the value was read, else 0."""
    from services.data_store_service import value_size
    buckets: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        ns = doc.get("namespace") or "default"
        b = buckets.setdefault(ns, {"recordCount": 0, "sizeBytes": 0, "agents": set(), "updatedAt": None})
        b["recordCount"] += 1
        if doc.get("sizeBytes") is not None:
            b["sizeBytes"] += doc["sizeBytes"]
        elif "value" in doc:
            b["sizeBytes"] += value_size(doc.get("value"))
        for field in ("createdByAgent", "lastAccessedByAgent"):
            if doc.get(field):
                b["agents"].add(doc[field])
        updated = doc.get("updatedAt")
        if updated and (b["updatedAt"] is None or updated > b["updatedAt"]):
            b["updatedAt"] = updated
    return {ns: {**b, "agents": sorted(b["agents"])} for ns, b in buckets.items()}


def totals_by_find(db, user_id: str, namespace: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    selector = {"userId": user_id}
    if namespace is not None:
        selector["namespace"] = namespace
    return totals_from_docs(db.find(DATA_STORE_DB, selector, fields=STATS_FIELDS, limit=None))
