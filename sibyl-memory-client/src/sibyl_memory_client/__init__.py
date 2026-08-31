"""sibyl-memory-client - Local-first agentic memory SDK.

Public exports:
    MemoryClient        the main interface
    Storage             low-level connection wrapper (advanced use)
    DEFAULT_TENANT      canonical single-user tenant UUID
    Exceptions          SibylMemoryError + subclasses
    Learner             self-learning pattern detector (v0.2.0)
    SkillProposal       review-queue dataclass (v0.2.0)
    LearningRunReport   summary of a learning pass (v0.2.0)
    Summarizer*         pluggable LLM backends (v0.2.0)
    Linter              local memory linter (v0.2.0)
    LintReport          aggregated lint output (v0.2.0)
    Finding             single lint finding (v0.2.0)

Quickstart:

    from sibyl_memory_client import MemoryClient
    client = MemoryClient.local("~/.sibyl-memory/memory.db")
    client.set_entity("project", "atlas", {"status": "active", "stage": "staging"})
    client.write_event(acted=["deployed atlas v1.2"])

    # Self-learning (v0.2.0): scan journal, propose skills, review queue
    report = client.learn()
    for proposal in client.list_skill_proposals():
        print(proposal.proposed_title, proposal.confidence)
        client.accept_skill_proposal(proposal.id)  # → writes reference/skill/<slug>

    # Memory linter (v0.2.0):
    print(client.lint().to_ascii())
"""
from ._capcheck import (
    CapExceededError,
    CapGate,
    FREE_TIER_CAP_BYTES,
    GRACE_PERIOD_SECONDS,
    TierCache,
    TierCacheEntry,
    TierVerificationError,
)
from .client import DEFAULT_TENANT, MemoryClient
from .exceptions import (
    ConflictError,
    NotFoundError,
    SchemaError,
    SibylMemoryError,
    StorageError,
    TenantError,
    TierGateError,
    ValidationError,
)
from .learning import (
    BYOKSummarizer,
    Learner,
    LearningRunReport,
    LocalDeterministicSummarizer,
    SkillProposal,
    Summarizer,
    VeniceX402Summarizer,
)
from .lint import Finding, LintReport, Linter
from .storage import Storage
# The verdict contract (stage 3, 2026-08-31). THE canonical cause vocabulary for
# the whole package family — mcp, hermes, cli and langgraph import these names
# from here, and none of them declares a cause string of its own. Re-declaration
# is how a surface drifts from the engine and starts reporting a cause the engine
# never emitted.
from .verdicts import (
    GateCause,
    GateCounters,
    SearchResults,
    Verdict,
    VerdictCode,
    ZERO_CAUSES,
    explain,
    refine_zero,
)

# Single-sourced from installed metadata so the wheel + code never drift
# (C2 audit fix v0.3.3). Source-tree fallback for editable installs that
# haven't been pip-installed yet.
from importlib.metadata import PackageNotFoundError, version as _pkg_version
try:
    __version__ = _pkg_version("sibyl-memory-client")
except PackageNotFoundError:  # pragma: no cover - source-tree dev only
    __version__ = "0.0.0+source"

__all__ = [
    # core
    "MemoryClient",
    "Storage",
    "DEFAULT_TENANT",
    # exceptions
    "SibylMemoryError",
    "StorageError",
    "SchemaError",
    "TenantError",
    "NotFoundError",
    "ConflictError",
    "ValidationError",
    "TierGateError",
    # cap enforcement (v0.3.0)
    "CapExceededError",
    "TierVerificationError",
    "CapGate",
    "TierCache",
    "TierCacheEntry",
    "FREE_TIER_CAP_BYTES",
    "GRACE_PERIOD_SECONDS",
    # learning (v0.2.0)
    "Learner",
    "SkillProposal",
    "LearningRunReport",
    "Summarizer",
    "LocalDeterministicSummarizer",
    "BYOKSummarizer",
    "VeniceX402Summarizer",
    # lint (v0.2.0)
    "Linter",
    "LintReport",
    "Finding",
    # verdict contract (stage 3, 2026-08-31)
    "SearchResults",
    "Verdict",
    "VerdictCode",
    "GateCause",
    "GateCounters",
    "ZERO_CAUSES",
    "explain",
    "refine_zero",
    # meta
    "__version__",
]
