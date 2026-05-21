"""Smoke tests for sibyl_memory_client.learning."""
from __future__ import annotations

from pathlib import Path

import pytest

from sibyl_memory_client import (
    BYOKSummarizer,
    Learner,
    LearningRunReport,
    LocalDeterministicSummarizer,
    MemoryClient,
    SkillProposal,
    VeniceX402Summarizer,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

@pytest.fixture
def client(tmp_path: Path) -> MemoryClient:
    db = tmp_path / "memory.db"
    # Self-learning is paid-tier only. Tests run as a lifetime-tier user.
    return MemoryClient.local(str(db), tier="lifetime")


def _seed_repeated_action(client: MemoryClient, n: int = 4) -> None:
    """Write N events with the same action signature."""
    for i in range(n):
        client.write_event(
            evaluated={"task": "fix bug", "ticket": f"TASK-{i}"},
            acted=["deployed atlas to staging"],
        )


def _seed_structural_pattern(client: MemoryClient, n: int = 3) -> None:
    """Write N events with the same evaluated key set."""
    for i in range(n):
        client.write_event(
            evaluated={"step": i, "module": "auth", "owner": "jane"},
            acted={"kind": f"checkpoint-{i}"},
        )


# ----------------------------------------------------------------------
# Schema migration v1 → v2 (the new tables must exist after open)
# ----------------------------------------------------------------------
def test_schema_v2_applied(client: MemoryClient) -> None:
    assert client.schema_version() >= 2
    # Tables should be queryable without error
    proposals = client.list_skill_proposals()
    assert proposals == []


# ----------------------------------------------------------------------
# Learner basics
# ----------------------------------------------------------------------
def test_learner_no_events_no_proposals(client: MemoryClient) -> None:
    report = client.learn()
    assert isinstance(report, LearningRunReport)
    assert report.events_scanned == 0
    assert report.proposals_made == 0
    assert report.summarizer == "local-deterministic"


def test_learner_detects_repeated_action(client: MemoryClient) -> None:
    _seed_repeated_action(client, n=4)
    report = client.learn()
    assert report.events_scanned >= 4
    assert report.proposals_made >= 1

    proposals = client.list_skill_proposals()
    kinds = {p.pattern_kind for p in proposals}
    assert "repeated_action" in kinds
    rep = next(p for p in proposals if p.pattern_kind == "repeated_action")
    assert rep.confidence > 0.4
    assert rep.summarizer == "local-deterministic"
    assert "deployed" in rep.proposed_body.lower()


def test_learner_watermark_no_double_propose(client: MemoryClient) -> None:
    _seed_repeated_action(client, n=4)
    first = client.learn()
    assert first.proposals_made >= 1
    # Second run with no new events should skip
    second = client.learn()
    assert second.events_scanned == 0
    assert second.proposals_made == 0


def test_learner_detects_structural_similarity(client: MemoryClient) -> None:
    _seed_structural_pattern(client, n=3)
    report = client.learn()
    proposals = client.list_skill_proposals()
    kinds = {p.pattern_kind for p in proposals}
    # Should at least pick up the shape
    assert "structural_similarity" in kinds or "co_occurrence" in kinds


# ----------------------------------------------------------------------
# Review queue: accept / reject
# ----------------------------------------------------------------------
def test_accept_proposal_writes_reference(client: MemoryClient) -> None:
    _seed_repeated_action(client, n=4)
    client.learn()
    proposals = client.list_skill_proposals()
    assert proposals

    target = proposals[0]
    result = client.accept_skill_proposal(target.id, note="useful")
    assert result["accepted"] is True
    assert result["doc_key"].startswith("skill/")

    # Reference doc landed
    ref = client.get_reference(result["doc_key"])
    assert ref is not None
    assert target.proposed_body == ref["body"]

    # Proposal status updated
    after = client.list_skill_proposals(status="accepted")
    assert any(p.id == target.id for p in after)


def test_reject_proposal_does_not_write_reference(client: MemoryClient) -> None:
    _seed_repeated_action(client, n=4)
    client.learn()
    proposals = client.list_skill_proposals()
    target = proposals[0]

    result = client.reject_skill_proposal(target.id, note="not useful")
    assert result["rejected"] is True

    # No skill/<slug> reference doc should exist
    assert client.get_reference(f"skill/{target.proposed_slug}") is None

    # Proposal removed from pending
    pending = client.list_skill_proposals(status="pending")
    assert not any(p.id == target.id for p in pending)


def test_double_accept_raises(client: MemoryClient) -> None:
    _seed_repeated_action(client, n=4)
    client.learn()
    target = client.list_skill_proposals()[0]
    client.accept_skill_proposal(target.id)
    with pytest.raises(Exception):
        client.accept_skill_proposal(target.id)


# ----------------------------------------------------------------------
# Custom summarizer plumbing. BYOK + Venice/x402 stubs
# ----------------------------------------------------------------------
def test_byok_summarizer_invokes_inference_fn(client: MemoryClient) -> None:
    captured = {}

    def fake_inference(prompt: str) -> str:
        captured["prompt"] = prompt
        return "# Skill from BYOK\n\nDo the thing."

    summarizer = BYOKSummarizer(fake_inference, provider_label="testlab")
    assert summarizer.name == "byok-testlab"

    _seed_repeated_action(client, n=4)
    learner = client.learner(summarizer=summarizer)
    report = learner.run()
    assert report.summarizer == "byok-testlab"
    assert report.proposals_made >= 1

    # The summarizer was called with the journal context
    assert "prompt" in captured
    assert "behavioral pattern" in captured["prompt"]

    proposals = learner.list_proposals()
    assert any("Skill from BYOK" in p.proposed_body for p in proposals)


def test_venice_x402_summarizer_fallback_on_error(client: MemoryClient) -> None:
    def bad_inference(prompt: str) -> str:
        raise RuntimeError("simulated network failure")

    summarizer = VeniceX402Summarizer(bad_inference, account_id="acc-stub")
    _seed_repeated_action(client, n=4)
    learner = client.learner(summarizer=summarizer)
    report = learner.run()
    assert report.proposals_made >= 1

    proposals = learner.list_proposals()
    # Fallback note should be present
    assert any("Venice/x402 call failed" in p.proposed_body for p in proposals)


# ----------------------------------------------------------------------
# Multi-tenant isolation
# ----------------------------------------------------------------------
def test_learner_is_tenant_scoped(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    alice = MemoryClient.local(str(db), tenant_id="alice", tier="lifetime")
    bob = MemoryClient.local(str(db), tenant_id="bob", tier="lifetime")

    _seed_repeated_action(alice, n=4)
    alice.learn()

    # Bob has not learned anything; should see zero proposals
    bobs_proposals = bob.list_skill_proposals()
    assert bobs_proposals == []

    # Alice has at least one
    alice_proposals = alice.list_skill_proposals()
    assert alice_proposals
    for p in alice_proposals:
        assert p.tenant_id == "alice"


# ----------------------------------------------------------------------
# Tier gating: free tier blocked from self-learning
# ----------------------------------------------------------------------
def test_free_tier_cannot_learn(tmp_path: Path) -> None:
    from sibyl_memory_client import TierGateError
    free = MemoryClient.local(str(tmp_path / "free.db"))  # default tier="free"
    with pytest.raises(TierGateError) as exc:
        free.learn()
    assert exc.value.feature == "self-learning"
    assert exc.value.current_tier == "free"


def test_free_tier_cannot_list_proposals(tmp_path: Path) -> None:
    from sibyl_memory_client import TierGateError
    free = MemoryClient.local(str(tmp_path / "free.db"))
    with pytest.raises(TierGateError):
        free.list_skill_proposals()


def test_free_tier_can_still_use_core_memory(tmp_path: Path) -> None:
    """Free-tier users get the full memory SDK: only learning/lint are gated.
    This is the upgrade-pressure design: free tier is fully functional storage
    + retrieval, paid tier adds the intelligence layer."""
    free = MemoryClient.local(str(tmp_path / "free.db"))
    free.set_entity("project", "atlas", {"status": "active"})
    free.write_event(acted=["did something"])
    free.set_state("priorities", {"top": ["ship"]})
    free.set_reference("rule-1", "always ship")

    # All core reads work
    assert free.get_entity("project", "atlas")["body"]["status"] == "active"
    assert free.get_state("priorities") is not None
    assert free.get_reference("rule-1") is not None
    assert free.read_events()
    # FTS5 search works
    results = free.search_entities("atlas")
    assert results


def test_paid_tier_upgrade_unlocks_learn(tmp_path: Path) -> None:
    """Simulate upgrade flow: start free, set_tier('lifetime'), learn now works."""
    client = MemoryClient.local(str(tmp_path / "u.db"))
    _seed_repeated_action(client, n=4)

    # Free tier blocks
    from sibyl_memory_client import TierGateError
    with pytest.raises(TierGateError):
        client.learn()

    # Upgrade → unlock
    client.set_tier("lifetime")
    report = client.learn()
    assert report.proposals_made >= 1
