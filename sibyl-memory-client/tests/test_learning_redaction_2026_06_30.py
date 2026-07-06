"""Sibyl-routed summarizer redaction regression (#14, B005, 2026-06-30).

The self-learning module's privacy contract: on the Sibyl Labs-hosted
inference path (VeniceX402Summarizer), "only the prompt summary leaves the
device, never the underlying memory content." The prompt builder used to embed
full journal-event payloads (events[:10]) for every path. This guards that:

  1. the Sibyl-routed prompt carries ONLY metadata (keys / counts / timestamps),
     never raw journal-event content;
  2. the BYOK path is unaffected and keeps full event fidelity (the user owns
     the inference destination).
"""
from __future__ import annotations

from pathlib import Path

from sibyl_memory_client import (
    BYOKSummarizer,
    MemoryClient,
    VeniceX402Summarizer,
)

# Distinctive raw-content markers seeded into the journal. None of these strings
# may appear in a Sibyl-routed prompt; all should survive into a BYOK prompt.
_SECRET_TASK = "exfiltrate-the-quarterly-revenue-figures"
_SECRET_TICKET = "TICKET-classified-9f3a"
_SECRET_ACTION = "wired funds to acct 4471-secret"


def _seed_with_secrets(client: MemoryClient, n: int = 4) -> None:
    for _ in range(n):
        client.write_event(
            evaluated={"task": _SECRET_TASK, "ticket": _SECRET_TICKET},
            acted=[_SECRET_ACTION],
        )


def _client(tmp_path: Path) -> MemoryClient:
    return MemoryClient.local(str(tmp_path / "m.db"), tier="lifetime")


def test_sibyl_routed_prompt_redacts_raw_content(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def capture_inference(prompt: str) -> str:
        captured["prompt"] = prompt
        return "# Skill\n\nDo the thing."

    summarizer = VeniceX402Summarizer(capture_inference, account_id="acc-stub")
    client = _client(tmp_path)
    _seed_with_secrets(client)

    report = client.learner(summarizer=summarizer).run()
    assert report.proposals_made >= 1
    assert "prompt" in captured

    prompt = captured["prompt"]
    # No raw memory content reaches the Sibyl-routed prompt.
    assert _SECRET_TASK not in prompt
    assert _SECRET_TICKET not in prompt
    assert _SECRET_ACTION not in prompt
    # Hardening #1 (super-patch 2026-07-05): dict KEY NAMES are content and must
    # NOT reach the Sibyl-routed prompt (content can hide in a key name just as
    # easily as in a value). The evaluated payload is now reduced to a count +
    # per-key lengths, never the literal key names.
    assert "key_count" in prompt  # shape marker proves redaction ran
    assert "ticket" not in prompt  # an evaluated key name -- must not leak
    assert "metadata only" in prompt
    # Strengthened (audit 2026-06-30): the prior version checked only the full
    # raw strings, so normalized hint derivatives (action_signature = first-N
    # hyphenated tokens of `acted`, plus pair/slug) slipped through. Assert no
    # acted-derived content fragment reaches the Sibyl-routed prompt...
    for token in ("wired", "funds", "acct", "4471"):
        assert token not in prompt, f"acted-derived token {token!r} leaked via hints"
    # ...while confirming a content-derived hint is retained as a KEY but reduced
    # to a shape stub (so the token checks above are non-vacuous: the hint really
    # is sent, just redacted). `slug` is emitted by every detector pattern and is
    # derived from raw content, so it must appear reduced to a {"type":"str"} stub.
    assert '"slug"' in prompt
    assert '"type": "str"' in prompt


def test_byok_prompt_keeps_full_fidelity(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def capture_inference(prompt: str) -> str:
        captured["prompt"] = prompt
        return "# Skill\n\nDo the thing."

    summarizer = BYOKSummarizer(capture_inference, provider_label="testlab")
    client = _client(tmp_path)
    _seed_with_secrets(client)

    report = client.learner(summarizer=summarizer).run()
    assert report.proposals_made >= 1
    assert "prompt" in captured

    prompt = captured["prompt"]
    # BYOK destination is user-controlled, so full content is included.
    assert _SECRET_TASK in prompt
    assert _SECRET_ACTION in prompt
    assert "metadata only" not in prompt
