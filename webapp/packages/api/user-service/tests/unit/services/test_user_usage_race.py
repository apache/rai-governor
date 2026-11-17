"""Two calls by the same user recording usage at once: neither charge is lost."""

import copy

import pytest
from fastapi import HTTPException

from services.user_service import UserService


class RevDB:
    """Returns copies and answers a save with a stale _rev with 409, like CouchDB.
    ``between`` runs another writer just before a save: the race."""

    def __init__(self):
        self.docs = {}
        self.between = []

    def get(self, db, doc_id):
        if (db, doc_id) not in self.docs:
            raise HTTPException(status_code=404, detail="not found")
        return copy.deepcopy(self.docs[(db, doc_id)])

    def save(self, db, doc_id, doc):
        if self.between:
            self.between.pop(0)()
        current = self.docs.get((db, doc_id))
        if current is not None and doc.get("_rev") != current["_rev"]:
            raise HTTPException(status_code=409, detail="conflict")
        doc = copy.deepcopy(doc)
        doc["_rev"] = str(int(current["_rev"]) + 1 if current else 1)
        self.docs[(db, doc_id)] = doc
        return {"id": doc_id, "rev": doc["_rev"]}


def test_concurrent_charges_are_both_recorded():
    db = RevDB()
    svc = UserService(db)
    start = svc.get_user("u").usage_info.spend_remaining
    # The other call's charge lands between this one's read and its save.
    db.between.append(lambda: UserService(db).add_usage("u", 0.25))
    svc.add_usage("u", 0.5)
    user = svc.get_user("u")
    assert sorted(e.response_cost for e in user.usage_info.usage) == [0.25, 0.5]
    assert user.usage_info.spend_remaining == pytest.approx(start - 0.75)


def test_other_errors_are_not_retried():
    db = RevDB()
    svc = UserService(db)
    svc.get_user("u")

    def down():
        raise HTTPException(status_code=500, detail="down")

    db.between.append(down)
    with pytest.raises(HTTPException) as e:
        svc.add_usage("u", 0.5)
    assert e.value.status_code == 500
