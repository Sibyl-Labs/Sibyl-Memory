"""Recall neighborhood primitives (0.10.0): owners_of, hub_members, doc_neighborhood.

A document entity is "owned" by every hub entity that links to it with a
`source_of` edge. owners_of orders the owners by (doc_count, kind rank, key);
doc_count is the exact count up to _OWNER_COUNT_CAP and the hub card's
body.doc_count above it. hub_members lists a hub's documents (limit + 1 rows so
the caller can tell truncation). doc_neighborhood builds the recall payload.
"""
from __future__ import annotations

import pytest

from sibyl_memory_client import MemoryClient, entity_ref
from sibyl_memory_client import _links
from sibyl_memory_client.testing import NullEntitlementGate

OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _client(tmp_path, tenant=None) -> MemoryClient:
    kw = {"tenant_id": tenant} if tenant else {}
    return MemoryClient.local(str(tmp_path / "m.db"), entitlement_gate=NullEntitlementGate(), **kw)


def _own(c: MemoryClient, kind: str, key: str, docs: list[str], body=None) -> None:
    c.set_entity(kind, key, body if body is not None else {"card": key})
    for i, d in enumerate(docs):
        c.link(entity_ref(kind, key), entity_ref("email", d), "source_of",
               metadata={"date": f"2026-01-{i + 1:02d}", "role": "sender", "title": f"{d} title"})


@pytest.fixture
def g(tmp_path) -> MemoryClient:
    c = _client(tmp_path)
    for d in ("d1", "d2", "d3", "d4", "lonely"):
        c.set_entity("email", d, {"text": d})
    _own(c, "customer", "Acme", ["d1", "d2", "d3"])
    _own(c, "person", "Ann", ["d1", "d2"])
    _own(c, "ticket", "T-1", ["d1"])
    _own(c, "ticket", "T-2", ["d2"])
    _own(c, "project", "P", ["d2"])
    return c


def _owners(c, key):
    with c.storage.connection() as conn:
        return _links.owners_of(conn, c._tenant_id, [("entity", "email", key)])[("entity", "email", key)]


def test_owners_order_is_doc_count_then_kind_rank_then_key(g) -> None:
    assert [(o["kind"], o["key"], o["doc_count"]) for o in _owners(g, "d1")] == [
        ("ticket", "T-1", 1), ("person", "Ann", 2), ("customer", "Acme", 3)]
    # equal doc_count: ticket (rank 0) before project (rank 2)
    assert [(o["kind"], o["key"]) for o in _owners(g, "d2")] == [
        ("ticket", "T-2"), ("project", "P"), ("person", "Ann"), ("customer", "Acme")]
    assert _owners(g, "lonely") == []


def test_owner_count_above_cap_takes_the_card_doc_count(g, monkeypatch) -> None:
    monkeypatch.setattr(_links, "_OWNER_COUNT_CAP", 2)
    g.set_entity("customer", "Acme", {"doc_count": 500})          # card count above the cap
    acme = [o for o in _owners(g, "d1") if o["key"] == "Acme"][0]
    assert acme["doc_count"] == 500
    g.set_entity("customer", "Acme", {"doc_count": 1})            # unusable (not above the cap)
    acme = [o for o in _owners(g, "d1") if o["key"] == "Acme"][0]
    assert acme["doc_count"] == 3                                 # exact COUNT fallback
    g.set_entity("customer", "Acme", {"card": "no count"})
    acme = [o for o in _owners(g, "d1") if o["key"] == "Acme"][0]
    assert acme["doc_count"] == 3
    # at or under the cap the exact count is used whatever the card says
    ann = [o for o in _owners(g, "d1") if o["key"] == "Ann"][0]
    assert ann["doc_count"] == 2


def test_hub_members_excludes_and_reads_limit_plus_one(g) -> None:
    with g.storage.connection() as conn:
        rows = _links.hub_members(conn, g._tenant_id, "customer", "Acme", limit=1, exclude_key=None)
        assert [r["dsid"] for r in rows] == ["d1", "d2"]          # limit + 1
        rows = _links.hub_members(conn, g._tenant_id, "customer", "Acme", limit=5, exclude_key="d2")
        assert [r["dsid"] for r in rows] == ["d1", "d3"]
        r = rows[0]
        assert set(r) == {"dsid", "category", "date", "role", "title"}
        assert r["category"] == "email" and r["role"] == "sender" and r["date"] == "2026-01-01"


def test_hub_members_title_cut_to_100(g) -> None:
    g.set_entity("email", "long", {})
    g.set_entity("customer", "Big", {})
    g.link(entity_ref("customer", "Big"), entity_ref("email", "long"), "source_of",
           metadata={"title": "x" * 300})
    with g.storage.connection() as conn:
        rows = _links.hub_members(conn, g._tenant_id, "customer", "Big", limit=5, exclude_key=None)
    assert len(rows[0]["title"]) == 100


def test_doc_neighborhood_none_without_owner(g) -> None:
    assert g.doc_neighborhood("email", "lonely") is None
    assert g.doc_neighborhood("email", "missing") is None


def test_doc_neighborhood_hubs_siblings_and_caps(g) -> None:
    nb = g.doc_neighborhood("email", "d1")
    assert [h["key"] for h in nb["hubs"]] == ["T-1", "Ann", "Acme"]
    by_hub = {s["hub"]["key"]: s for s in nb["siblings"]}
    assert [r["dsid"] for r in by_hub["Acme"]["rows"]] == ["d2", "d3"]   # d1 itself excluded
    assert by_hub["Acme"]["total"] == 2 and by_hub["T-1"]["rows"] == []
    assert nb["siblings_omitted"] == 0
    # small_hub_max: hubs above it give no siblings (and are not counted omitted)
    nb = g.doc_neighborhood("email", "d1", small_hub_max=2)
    assert {s["hub"]["key"] for s in nb["siblings"]} == {"T-1", "Ann"}
    assert nb["siblings_omitted"] == 0 and len(nb["hubs"]) == 3
    # sibling_cap: a hub whose rows would pass the cap is omitted and counted
    nb = g.doc_neighborhood("email", "d1", sibling_cap=1)
    assert sum(len(s["rows"]) for s in nb["siblings"]) <= 1
    assert nb["siblings_omitted"] == 1                            # Acme (2 rows) left out


def test_doc_neighborhood_hubs_capped_at_ten(tmp_path) -> None:
    c = _client(tmp_path)
    c.set_entity("email", "d", {})
    for i in range(12):
        _own(c, "person", f"p{i:02d}", ["d"])
    nb = c.doc_neighborhood("email", "d")
    assert len(nb["hubs"]) == 10
    assert len(nb["siblings"]) == 12 and all(s["rows"] == [] for s in nb["siblings"])


def test_tenant_boundary(tmp_path, g) -> None:
    other = MemoryClient.local(str(tmp_path / "m.db"), tenant_id=OTHER,
                               entitlement_gate=NullEntitlementGate())
    other.set_entity("email", "d1", {})
    other.set_entity("customer", "Evil", {})
    other.link(entity_ref("customer", "Evil"), entity_ref("email", "d1"), "source_of")
    assert "Evil" not in {o["key"] for o in _owners(g, "d1")}
    assert [h["key"] for h in other.doc_neighborhood("email", "d1")["hubs"]] == ["Evil"]
    assert other.doc_neighborhood("email", "d2") is None
