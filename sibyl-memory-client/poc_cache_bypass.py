import json, tempfile, time
from pathlib import Path
from sibyl_memory_client._capcheck import CapGate, TierCache

def check_fn_that_must_not_be_called(url, payload, timeout=4.0):
    raise AssertionError("NETWORK CALL HAPPENED — bypass failed")

with tempfile.TemporaryDirectory() as d:
    cache_path = Path(d) / "tier_cache.json"
    fake_account_id = "totally-fake-account-id-attacker-picked"

    forged = {
        "account_id": fake_account_id,
        "tier": "lifetime",
        "checked_at": time.time(),
        "cap_bytes": None,
        "last_known_size": 0,
        "grace_seconds": 10 * 365 * 24 * 60 * 60,
        "server_expires_at": None,
        "cache_token": None,
    }
    cache_path.write_text(json.dumps(forged))

    gate = CapGate(
        account_id=fake_account_id,
        session_token=None,
        db_size_fn=lambda: 500 * 1024 * 1024,
        local_tier_hint="free",
        cache=TierCache(path=cache_path),
        check_fn=check_fn_that_must_not_be_called,
    )
    gate.check(proposed_delta_bytes=200 * 1024 * 1024)
    print("BYPASS CONFIRMED — 700MB write allowed, zero network calls")
    print("current_cap():", gate.current_cap())
