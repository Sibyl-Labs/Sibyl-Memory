#!/usr/bin/env bash
# Fresh-venv release gate for the coordinated plugin release (account-gated spec
# 6.3 / 7.1 step 10; extends the cli 0.5.0 CLI-3 gate). Not a pytest.
#
# Creates a clean venv, installs ALL FIVE built wheels from <dist-dir> (client,
# hermes, mcp, langgraph, cli; never the PyPI copies of these) plus the CURRENT
# hermes-agent from PyPI, then asserts:
#   * every command below exits 0 with no credentials;
#   * `sibyl memory list` exits 0 on a store and prints the no-account line;
#   * the MCP server answers memory_list with an isError result whose text is
#     JSON with code == "ACCOUNT_REQUIRED" (a ToolError envelope, NOT a Python
#     exception);
#   * the hermes adapter instantiates against the live hermes-agent
#     MemoryProvider ABC AND handle_tool_call("sibyl_list", {}) returns the
#     tool_error JSON (not an exception);
#   * SibylStore().put raises EntitlementError (langgraph propagates by design);
#   * fabricated credentials with the api unreachable (a closed loopback port)
#     are refused with setup_incomplete, also after entitlement.json is deleted
#     (no bootstrap grace);
#   * rows written under DEFAULT_TENANT before sign-in are readable and
#     searchable after sign-in (a throwaway test key, this process only);
#   * `import cryptography` resolves and is a declared client dependency;
#   * save/debloat (spec plugin-save-debloat 8.6): `sibyl debloat --fyi` on an
#     empty HOME, bare `sibyl save`, `sibyl skills install` + `status`,
#     `sibyl setup claude-code --dry-run` (two shims, five hooks, nothing
#     written), install-plugin writes the hermes skills, `sibyl setup codex
#     --dry-run` (two skill shims, nothing written), the console script starts
#     at _entry, `sibyl hook stop` exits 0 silently, the installed hook
#     command exits 0 silently once the binary is gone, the MCP server lists
#     the save/debloat prompts and four resources with no credentials and the
#     save prompt starts with the watermark, the adapter's prompt block names
#     SAVE.md; the hermes-agent skills dir is printed (informational);
#   * paid tier R1 (plan plugin-paid-tier-2026-09-29 9.2): `sibyl refresh` with
#     no account exits 0 and says to run sibyl init; `sibyl autosave status`;
#     `sibyl skills proposals` with no account exits 0 (nothing proposed); a
#     recorded 409 device_limit answer makes `sibyl init` print the computers
#     and the replace prompt (no network: the CLI's HTTP call is replaced);
#   * tier convention (spec tier-convention 2026-10-05): `sibyl status` with an
#     elite entitlement (a throwaway test key, this process only, no network)
#     prints `Tier  Elite` and exits 0;
#   * the pinned kid set in sibyl_memory_client/_entitlement_keys.py equals the
#     kids the operator read from production /api/plugin/health
#     (--expect-kids; no network in this script) and is not a PLACEHOLDER.
#
# Usage:
#   venv-compat.sh <dist-dir> --expect-kids <kid>[,<kid>] [--venv <dir>]
#   venv-compat.sh <dist-dir> --allow-placeholder-key [--venv <dir>]   # rehearsal only
# --allow-placeholder-key turns the pinned-key check into a WARN so the rest of
# the gate can be rehearsed before the production key exists; a release run
# must pass --expect-kids and must not pass --allow-placeholder-key.
#
# Runs with a throwaway HOME, so no real credentials, config or HERMES_HOME are
# read or written. Network: PyPI only (pip). No Sibyl service is called: with
# no credentials the account gate never reaches the network.
set -euo pipefail

DIST="${1:?usage: venv-compat.sh <dist-dir> (--expect-kids <kids> | --allow-placeholder-key) [--venv <dir>]}"
shift
EXPECT_KIDS=""
ALLOW_PLACEHOLDER=0
VENV=""
while [ $# -gt 0 ]; do
  case "$1" in
    --expect-kids) EXPECT_KIDS="${2:?--expect-kids needs a value}"; shift 2 ;;
    --allow-placeholder-key) ALLOW_PLACEHOLDER=1; shift ;;
    --venv) VENV="${2:?--venv needs a value}"; shift 2 ;;
    *) echo "unknown argument: $1"; exit 2 ;;
  esac
done
if [ -z "$EXPECT_KIDS" ] && [ "$ALLOW_PLACEHOLDER" -ne 1 ]; then
  echo "pass --expect-kids <kid>[,<kid>] (production /api/plugin/health), or --allow-placeholder-key for a rehearsal"
  exit 2
fi
VENV="${VENV:-$(mktemp -d)/venv}"
PY="${PYTHON:-python3}"

DIST="$(cd "$DIST" && pwd)"
WHEELS=()
for pkg in sibyl_memory_client sibyl_memory_hermes sibyl_memory_mcp sibyl_memory_langgraph sibyl_memory_cli; do
  w="$(ls "$DIST"/${pkg}-*-py3-none-any.whl 2>/dev/null | sort | tail -1 || true)"
  if [ -z "$w" ]; then echo "missing wheel for $pkg in $DIST"; exit 2; fi
  WHEELS+=("$w")
done
CLI_WHEEL="${WHEELS[4]}"

FAKE_HOME="$(mktemp -d)"
export HOME="$FAKE_HOME"
export HERMES_HOME="$FAKE_HOME/.hermes"
export SIBYL_NONINTERACTIVE=1
export NO_COLOR=1
unset SIBYL_API_BASE SIBYL_ACCOUNT_BASE SIBYL_DASHBOARD_BASE SIBYL_MEMORY_DB SIBYL_CREDENTIALS SIBYL_TENANT_ID || true

echo "== venv: $VENV"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install -q --upgrade pip

echo "== hermes-agent versions on PyPI (latest first):"
"$VENV/bin/python" -m pip index versions hermes-agent 2>/dev/null | head -2 || echo "  (pip index unavailable)"

echo "== install: the five built wheels + current hermes-agent"
"$VENV/bin/python" -m pip install -q --find-links "$DIST" "${WHEELS[@]}" hermes-agent

"$VENV/bin/python" -m pip list 2>/dev/null | grep -Ei '^(sibyl-memory-[a-z]+|hermes-agent|cryptography) ' | sed 's/^/  /'

fail=0
run() {
  local label="$1"; shift
  if out="$("$@" 2>&1)"; then
    echo "  ok    $label"
  else
    echo "  FAIL  $label (exit $?)"; echo "$out" | tail -15 | sed 's/^/        /'; fail=1
  fi
}

B="$VENV/bin"
echo "== commands (no credentials)"
run "sibyl --help"                   "$B/sibyl" --help
run "sibyl status --help"            "$B/sibyl" status --help
run "sibyl email --help"             "$B/sibyl" email --help
run "sibyl init --help (--no-email)" bash -c "'$B/sibyl' init --help | grep -q -- --no-email"
run "sibyl status (no creds)"        "$B/sibyl" status
run "sibyl email (no creds)"         "$B/sibyl" email
run "sibyl health"                   "$B/sibyl" health
run "sibyl setup --help"             "$B/sibyl" setup --help
run "cli version is the wheel's"     "$B/python" -c "import sibyl_memory_cli as m, sys; w='$(basename "$CLI_WHEEL")'; sys.exit(0 if w.split('-')[1]==m.__version__ else 1)"
run "adapters import"                "$B/python" -c "import sibyl_memory_client, sibyl_memory_hermes, sibyl_memory_mcp, sibyl_memory_langgraph"
run "sibyl status says agents need an account" bash -c "'$B/sibyl' status | grep -q 'agents cannot use memory until you do'"

echo "== account gate"
run "cryptography is a declared client dependency and imports" "$B/python" - <<'PYEOF'
import importlib.metadata as md
reqs = md.requires("sibyl-memory-client") or []
assert any(r.split(";")[0].replace(" ", "").lower().startswith("cryptography") for r in reqs), reqs
import cryptography
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: F401
print("  cryptography", cryptography.__version__)
PYEOF

run "sibyl memory list reads without an account" bash -c "
'$B/python' - <<'PYEOF'
from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
from sibyl_memory_client.testing import NullEntitlementGate
c = MemoryClient.local('$FAKE_HOME/.sibyl-memory/memory.db', tenant_id=DEFAULT_TENANT,
                       entitlement_gate=NullEntitlementGate())
c.set_entity('compat', 'probe', {'note': 'written before an account existed'})
PYEOF
out=\$('$B/sibyl' memory list) && echo \"\$out\" | grep -q 'compat/probe' && echo \"\$out\" | grep -q 'reading local data without an account'"

run "mcp: memory_list -> isError ACCOUNT_REQUIRED (not an exception)" "$B/python" - <<'PYEOF'
import asyncio, json
from mcp.types import CallToolRequest
from sibyl_memory_mcp.server import build_server
mcp = build_server()
handler = mcp._mcp_server.request_handlers[CallToolRequest]
res = asyncio.run(handler(CallToolRequest(method="tools/call",
                                          params={"name": "memory_list", "arguments": {}})))
root = res.root
assert root.isError is True, root
payload = json.loads(root.content[0].text)
assert payload["code"] == "ACCOUNT_REQUIRED" and payload["reason"] == "no_account", payload
tools = asyncio.run(mcp.list_tools())
assert len(tools) == 8, [t.name for t in tools]
print("  mcp", payload["code"], payload["reason"], "| tools listed:", len(tools))
PYEOF

run "sibyl-memory-hermes install-plugin" "$B/sibyl-memory-hermes" install-plugin --hermes-home "$HERMES_HOME"
run "adapter vs live hermes-agent ABC + signed-out tool call" "$B/python" - <<PYEOF
import importlib.util, glob, json
# install-plugin copies the bundled adapter to \$HERMES_HOME/plugins/sibyl/__init__.py
paths = sorted(glob.glob("$HERMES_HOME/plugins/sibyl/__init__.py"))
assert paths, "installed plugin not found at HERMES_HOME/plugins/sibyl/__init__.py"
for p in paths:
    spec = importlib.util.spec_from_file_location("sibyl_adapter_under_test", p)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert mod._HERMES_AVAILABLE, "agent.memory_provider not importable from hermes-agent"
    from agent.memory_provider import MemoryProvider
    assert issubclass(mod.SibylAdapter, MemoryProvider)
    inst = mod.SibylAdapter()            # raises TypeError if an abstract method is missing
    assert inst.is_available() is True
    inst.initialize("compat-session", hermes_home="$HERMES_HOME")
    out = inst.handle_tool_call("sibyl_list", {})
    assert isinstance(out, str), type(out)
    body = json.loads(out)
    assert "sibyl init" in json.dumps(body), body
    blk = inst.system_prompt_block()
    assert "~/.sibyl-memory/skills/SAVE.md" in blk and "DEBLOAT.md" in blk, blk[-400:]
    print("  adapter", p, "->", type(inst).__name__, "name =", inst.name, "| tool_error:", body,
          "| prompt block names SAVE.md")
PYEOF

echo "== save and debloat (spec plugin-save-debloat 8.6)"
# PATH without the operator's own tools, so no real `claude` binary is ever
# consulted from this throwaway HOME.
SPATH="$VENV/bin:/usr/bin:/bin"
run "sibyl debloat --fyi on an empty HOME" bash -c "cd '$FAKE_HOME' && out=\$(env PATH='$SPATH' '$B/sibyl' debloat --fyi) && echo \"\$out\" | grep -q 'no always-load files found'"
run "sibyl save (bare) exits 0 and writes nothing" bash -c "cd '$FAKE_HOME' && before=\$(ls -la --time-style=full-iso '$FAKE_HOME/.sibyl-memory' 2>/dev/null | sha256sum) && env PATH='$SPATH' '$B/sibyl' save | grep -q '/sibyl-save' && after=\$(ls -la --time-style=full-iso '$FAKE_HOME/.sibyl-memory' 2>/dev/null | sha256sum) && test \"\$before\" = \"\$after\""
run "sibyl skills install + status list both files" bash -c "cd '$FAKE_HOME' && env PATH='$SPATH' '$B/sibyl' skills install >/dev/null && out=\$(env PATH='$SPATH' '$B/sibyl' skills status) && echo \"\$out\" | grep -q 'SAVE.md' && echo \"\$out\" | grep -q 'DEBLOAT.md' && test -f '$FAKE_HOME/.sibyl-memory/skills/SAVE.md'"
run "sibyl setup claude-code --dry-run: two shims, five hooks, nothing written" bash -c "cd '$FAKE_HOME' && mkdir -p '$FAKE_HOME/.claude' && out=\$(env PATH='$SPATH' '$B/sibyl' setup claude-code --dry-run --yes) ; echo \"\$out\" | grep -c 'would write shim' | grep -qx 2 && echo \"\$out\" | grep -c 'would add hook' | grep -qx 5 && test ! -e '$FAKE_HOME/.claude/settings.json' && test ! -e '$FAKE_HOME/.claude/skills'"
run "install-plugin wrote the hermes skills" bash -c "test -f '$HERMES_HOME/skills/sibyl-save/SKILL.md' && test -f '$HERMES_HOME/skills/sibyl-debloat/SKILL.md'"
run "sibyl setup codex --dry-run: two skill shims, nothing written" bash -c "cd '$FAKE_HOME' && mkdir -p '$FAKE_HOME/.codex' && out=\$(env PATH='$SPATH' '$B/sibyl' setup codex --dry-run --yes) ; echo \"\$out\" | grep -c 'would write shim .*/.codex/skills/sibyl-' | grep -qx 2 && test ! -e '$FAKE_HOME/.codex/skills' && test ! -e '$FAKE_HOME/.codex/AGENTS.md'"
run "the sibyl console script starts at sibyl_memory_cli._entry" grep -q 'sibyl_memory_cli._entry' "$B/sibyl"
run "sibyl hook stop from the wheel: exit 0, prints nothing" bash -c "cd '$FAKE_HOME' && out=\$(echo '{\"session_id\": \"compat\", \"stop_hook_active\": false}' | env PATH='$SPATH' '$B/sibyl' hook stop) && test -z \"\$out\""
run "the installed hook command exits 0 silently once sibyl is gone" bash -c "cmd=\$('$B/python' -c 'from sibyl_memory_cli.setup import hook_command; print(hook_command(\"/nonexistent-sibyl-compat/bin/sibyl\", \"stop\"))') && out=\$(echo '{}' | bash -c \"\$cmd\" 2>&1) && test -z \"\$out\""
run "mcp: prompts save/debloat and four resources with no credentials" "$B/python" - <<'PYEOF'
import asyncio
from sibyl_memory_mcp.server import build_server
m = build_server()
names = {p.name for p in asyncio.run(m.list_prompts())}
assert {"save", "debloat"} <= names, names
uris = {str(r.uri) for r in asyncio.run(m.list_resources())}
want = {"sibyl://skills/SAVE.md", "sibyl://skills/DEBLOAT.md", "sibyl://skills/manifest", "sibyl://context/weight"}
assert want <= uris, uris
text = asyncio.run(m.get_prompt("save", {})).messages[0].content.text
assert text.startswith("<!-- SIBYL-SKILL-WATERMARK-BEGIN"), text[:80]
print("  prompts", sorted(names), "| resources", len(want), "| save prompt starts with the watermark")
PYEOF
echo "  hermes skills dir (informational, D5): $("$B/python" -c 'from hermes_constants import get_skills_dir; print(get_skills_dir())' 2>/dev/null || echo 'not exposed by this hermes-agent')"

echo "== paid tier R1 (plan plugin-paid-tier-2026-09-29)"
run "sibyl refresh with no account exits 0" bash -c "cd '$FAKE_HOME' && out=\$(env PATH='$SPATH' '$B/sibyl' refresh) && echo \"\$out\" | grep -q 'Not activated'"
run "sibyl autosave status" bash -c "cd '$FAKE_HOME' && out=\$(env PATH='$SPATH' '$B/sibyl' autosave status) && echo \"\$out\" | grep -q 'every 60 min of active work' && echo \"\$out\" | grep -q 'automatic save is on Pro'"
run "sibyl skills proposals with no account: exit 0, nothing proposed" bash -c "cd '$FAKE_HOME' && out=\$(env PATH='$SPATH' '$B/sibyl' skills proposals) && echo \"\$out\" | grep -q 'Not activated' && ! echo \"\$out\" | grep -q 'candidate'"
run "a 409 device_limit answer makes sibyl init print the account-page prompt" "$B/python" - <<'PYEOF'
import builtins, io, contextlib
from sibyl_memory_cli import cli
body = {"code": "device_limit", "limit": 1, "tier": "free",
        "devices": [{"label": "work-laptop", "os_family": "macos", "last_seen_at": "2026-09-28T17:02:11.000Z"}],
        "manage_url": "https://account.sibyllabs.org/security", "upgrade_url": "https://sibyllabs.org/pro"}
def fake(method, path, **kw):
    if path == "/api/plugin/session-init":
        raise cli.HttpError(409, body, "https://api.sibyllabs.org" + path)
    raise SystemExit("unexpected call " + path)
cli.http_request = fake
cli._is_interactive = lambda: True
cli.webbrowser.open = lambda *a, **k: True
asked = []
builtins.input = lambda prompt="": asked.append(prompt) or "n"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli.main(["init", "--no-email"])
out = buf.getvalue()
assert rc == 2, rc
assert asked == ["Open the account page to sign work-laptop out? [y/N]: "], asked
assert "This terminal cannot sign another computer out" in out, out
assert "work-laptop" in out and "account.sibyllabs.org/security" in out, out
assert "Pro works on every computer you use: https://sibyllabs.org/pro" in out, out
assert "up to 10" not in out, out
print("  init at the limit: exit", rc, "| prompt:", asked[0].strip())
PYEOF

echo "== tier convention (spec tier-convention 2026-10-05)"
run "sibyl status with an elite entitlement prints Elite, exit 0 (test key, no network)" "$B/python" - <<PYEOF
import contextlib, io, pathlib, re
from sibyl_memory_cli import cli
from sibyl_memory_client import _entitlement as E
from sibyl_memory_client.testing import TestSigner, write_credentials, write_entitlement
signer = TestSigner()
E.TRUSTED_KEYS = dict(signer.trusted)                           # this process only
E.http_refresh = lambda a, t: E.RefreshOutcome("ok", token=signer.mint(tier="elite", cap=None, src="subscription"))
def offline(*a, **k):
    raise cli.HttpError(0, {}, "offline")
cli.http_request = offline
d = pathlib.Path("$FAKE_HOME/elite"); d.mkdir(parents=True, exist_ok=True)
write_credentials(d, tier="elite")
write_entitlement(d, signer.mint(tier="elite", cap=None, src="subscription"))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli.main(["--credentials", str(d / "credentials.json"), "--db", str(d / "memory.db"),
                   "--tier-cache", str(d / "tier_cache.json"), "status"])
out = buf.getvalue()
assert rc == 0, rc
assert re.search(r"Tier\s+Elite\b", out), out
assert "active" in out and "tier_unknown" not in out, out
print("  status: Tier Elite, exit", rc)
PYEOF

run "langgraph: SibylStore().put raises EntitlementError" "$B/python" - <<'PYEOF'
from sibyl_memory_client import EntitlementError
from sibyl_memory_langgraph import SibylStore
s = SibylStore()
try:
    s.put(("compat",), "k", {"x": 1})
except EntitlementError as e:
    assert e.code == "no_account"
    print("  langgraph EntitlementError", e.code)
else:
    raise SystemExit("put did not raise")
PYEOF

run "legacy credentials, api unreachable -> setup_incomplete (no bootstrap grace)" "$B/python" - <<PYEOF
import json, os, socket, pathlib
from sibyl_memory_client import EntitlementError, MemoryClient
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
os.environ["SIBYL_API_BASE"] = f"http://127.0.0.1:{port}"      # closed loopback port: "blocked"
d = pathlib.Path("$FAKE_HOME/legacy"); d.mkdir(parents=True, exist_ok=True)
(d / "credentials.json").write_text(json.dumps({"account_id": "fabricated-account",
                                                "session_token": "fabricated-session"}))
for attempt in range(2):
    (d / "entitlement.json").unlink(missing_ok=True)            # deleting it restarts nothing
    c = MemoryClient.local(str(d / "memory.db"), credentials_path=d / "credentials.json")
    try:
        c.set_entity("compat", "x", {"a": 1})
    except EntitlementError as e:
        assert e.code == "setup_incomplete", e.code
    else:
        raise SystemExit("fabricated credentials were granted access")
print("  setup_incomplete twice, nothing granted")
PYEOF

run "rows written before sign-in are readable after sign-in (test key)" "$B/python" - <<PYEOF
import pathlib
from sibyl_memory_client import DEFAULT_TENANT, MemoryClient, _entitlement as E
from sibyl_memory_client.testing import (NullEntitlementGate, TestSigner, write_credentials,
                                         write_entitlement, TEST_ACCOUNT_ID)
signer = TestSigner()
E.TRUSTED_KEYS = dict(signer.trusted)                           # this process only
E.http_refresh = lambda a, t: E.RefreshOutcome("transient", code="offline")
d = pathlib.Path("$FAKE_HOME/adopt"); d.mkdir(parents=True, exist_ok=True)
old = MemoryClient.local(str(d / "memory.db"), tenant_id=DEFAULT_TENANT,
                         entitlement_gate=NullEntitlementGate())
old.set_entity("compat", "before", {"note": "written before sign-in"})
old.storage.close()
write_credentials(d); write_entitlement(d, signer.mint())
c = MemoryClient.local(str(d / "memory.db"), tenant_id=TEST_ACCOUNT_ID,
                       credentials_path=d / "credentials.json")
assert c.get_entity("compat", "before")["body"] == {"note": "written before sign-in"}
assert [h["tier"] for h in c.search("written")] == ["entity"]
print("  adopted:", c.adoption.status, c.adoption.counts)
PYEOF

echo "== pinned entitlement keys"
KEYS_OUT="$("$B/python" - "$EXPECT_KIDS" <<'PYEOF'
import sys, hashlib
from sibyl_memory_client import _entitlement_keys as K
expect = {k.strip() for k in sys.argv[1].split(",") if k.strip()}
pinned = set(K.TRUSTED_KEYS)
bad = [k for k, v in K.TRUSTED_KEYS.items() if hashlib.sha256(bytes(v)).hexdigest()[:16] != k]
placeholder = sorted(pinned & set(getattr(K, "PLACEHOLDER_KIDS", ())))
print("pinned=" + ",".join(sorted(pinned)))
print("placeholder=" + ",".join(placeholder))
print("malformed=" + ",".join(bad))
print("match=" + ("yes" if expect and pinned == expect else "no"))
PYEOF
)"
echo "$KEYS_OUT" | sed 's/^/  /'
if echo "$KEYS_OUT" | grep -q '^malformed=.\+'; then echo "  FAIL  a pinned key does not match its kid"; fail=1; fi
if echo "$KEYS_OUT" | grep -q '^placeholder=.\+'; then
  if [ "$ALLOW_PLACEHOLDER" -eq 1 ]; then
    echo "  WARN  PLACEHOLDER key pinned (rehearsal run; a release run must not pass --allow-placeholder-key)"
  else
    echo "  FAIL  PLACEHOLDER key pinned: replace _entitlement_keys.py with the production public key"; fail=1
  fi
fi
if [ -n "$EXPECT_KIDS" ]; then
  if echo "$KEYS_OUT" | grep -q '^match=yes'; then echo "  ok    pinned kids == production kids ($EXPECT_KIDS)"
  else echo "  FAIL  pinned kids != production kids ($EXPECT_KIDS)"; fail=1; fi
fi

echo "== hermes-agent verified: $("$B/python" -m pip show hermes-agent 2>/dev/null | awk '/^Version:/{print $2}')"
rm -rf "$FAKE_HOME"
if [ "$fail" -ne 0 ]; then echo "== RESULT: FAIL"; exit 1; fi
echo "== RESULT: all checks passed"
