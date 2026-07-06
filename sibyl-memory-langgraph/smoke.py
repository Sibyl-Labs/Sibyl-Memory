"""Quick smoke test for SibylStore before the full sub-agent suite."""
import tempfile, os, sys
from sibyl_memory_langgraph import SibylStore

d = tempfile.mkdtemp()
store = SibylStore(path=os.path.join(d, "smoke.db"), tier="free")
ok = 0
fail = 0
def check(name, cond):
    global ok, fail
    if cond: ok += 1; print(f"  PASS {name}")
    else: fail += 1; print(f"  FAIL {name}")

# put / get
store.put(("memories", "u1"), "fact1", {"text": "operator prefers dark mode", "kind": "pref"})
store.put(("memories", "u1"), "fact2", {"text": "billing handled by stripe", "kind": "ops"})
store.put(("memories", "u2"), "fact1", {"text": "different user fact", "kind": "pref"})

it = store.get(("memories", "u1"), "fact1")
check("get returns Item", it is not None)
check("get value round-trips", it and it.value.get("text") == "operator prefers dark mode")
check("get namespace round-trips", it and it.namespace == ("memories", "u1"))
check("get key round-trips", it and it.key == "fact1")
check("get has timestamps", it and it.created_at is not None and it.updated_at is not None)

# missing get
check("missing get -> None", store.get(("memories", "u1"), "nope") is None)

# overwrite
store.put(("memories", "u1"), "fact1", {"text": "now prefers light mode", "kind": "pref"})
check("overwrite updates value", store.get(("memories", "u1"), "fact1").value["text"] == "now prefers light mode")

# namespace isolation
check("namespace isolation (u1 vs u2 same key differ)",
      store.get(("memories", "u1"), "fact1").value != store.get(("memories", "u2"), "fact1").value)

# search exact namespace
hits = store.search(("memories", "u1"), query="stripe")
check("search finds stripe in u1", any(h.key == "fact2" for h in hits))
check("search does not leak u2", all(h.namespace == ("memories", "u1") for h in hits))

# subtree search (prefix shorter than stored namespace)
sub = store.search(("memories",), query="mode")
check("subtree search spans u1+u2", any(h.namespace == ("memories", "u1") for h in sub))

# filter
filt = store.search(("memories", "u1"), filter={"kind": "ops"})
check("filter kind=ops returns only ops", all(h.value.get("kind") == "ops" for h in filt) and len(filt) >= 1)

# browse (no query)
browse = store.search(("memories", "u1"))
check("browse returns u1 items", len(browse) == 2)

# list_namespaces
ns = store.list_namespaces()
check("list_namespaces includes memories/u1", ("memories", "u1") in ns)
check("list_namespaces includes memories/u2", ("memories", "u2") in ns)

# list_namespaces max_depth
nd = store.list_namespaces(max_depth=1)
check("list_namespaces max_depth=1 collapses", ("memories",) in nd)

# delete
store.delete(("memories", "u2"), "fact1")
check("delete removes item", store.get(("memories", "u2"), "fact1") is None)

# namespace validation
try:
    store.put(("bad/elem",), "k", {"x": 1}); check("rejects '/' in namespace element", False)
except ValueError:
    check("rejects '/' in namespace element", True)

store.close()
print(f"\nSMOKE: {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
