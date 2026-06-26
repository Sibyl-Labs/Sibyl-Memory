"""Memory linter for sibyl-memory-client.

Mirrors the spirit of `scripts/memory-lint.mjs` in the SIBYL operator
codebase: scan the local memory state for structural drift, surface
findings with severity + recovery hints, exit non-zero from CLI when
critical findings exist.

Designed to be invoked by:
  • the plugin's `sibyl lint` CLI command (in sibyl-labs-cli)
  • a scheduled cron job from `sibyl init`
  • programmatic callers via `MemoryClient.lint()`

CHECKS (v0.2.0)
===============

Severity levels: `critical` | `warning` | `info`

| id                     | severity | what it catches                              |
|------------------------|----------|----------------------------------------------|
| schema-version         | critical | DB schema older than the package expects     |
| invalid-json-entity    | critical | entities.body parses to non-object           |
| invalid-json-state     | critical | state_documents.body parses to non-object    |
| invalid-json-journal   | critical | journal_events fields are not valid JSON     |
| duplicate-entity       | warning  | same entity name under multiple categories   |
| empty-reference        | warning  | reference_documents.body is empty            |
| stale-entity           | info     | entities not updated in >N days              |
| journal-without-acts   | info     | journal events with no evaluated/acted/extra |
| db-soft-cap            | warning  | DB size exceeds 80% of the soft cap (10 MB)  |
| fts-rowcount-mismatch  | warning  | FTS5 index count differs from entities count |
| flagged-actors-fresh   | info     | recent flagged_actors entries (≤ N days)     |

The check list is intentionally conservative for v0.2.0: easy to extend.
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .client import DEFAULT_TENANT
from .storage import Storage, db_size_bytes

# ----------------------------------------------------------------------
# Public types
# ----------------------------------------------------------------------

SEVERITIES = ("critical", "warning", "info")
# Free-tier soft cap. Tuned 2026-05-15 to land the power-user conversion event
# in roughly 1-2 weeks of real use. Paid tiers remove the cap entirely.
DEFAULT_SOFT_CAP_BYTES = 2 * 1024 * 1024  # 2 MB free-tier cap
DEFAULT_STALE_DAYS = 90
DEFAULT_FLAG_RECENCY_DAYS = 30
EXPECTED_SCHEMA_VERSION = 2

# Tier → soft cap mapping. None means uncapped.
TIER_SOFT_CAPS: dict[str, int | None] = {
    "free": 2 * 1024 * 1024,        # 2 MB
    "sync": None,                    # uncapped: paid subscription
    "team": None,                    # uncapped: paid subscription
    "lifetime": None,                # uncapped: one-time payment
    "stake": None,                   # uncapped: $SIBYL stake
    "enterprise": None,              # uncapped: annual contract
}


@dataclass
class Finding:
    """A single lint result."""
    check: str
    severity: str  # critical | warning | info
    message: str
    recovery: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class LintReport:
    """Aggregated lint output."""
    tenant_id: str
    db_path: str
    schema_version: int | None
    db_size_bytes: int
    counts: dict[str, int]
    findings: list[Finding]
    started_at: str
    completed_at: str

    @property
    def critical(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "critical"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def info(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "info"]

    @property
    def ok(self) -> bool:
        return not self.critical

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "db_path": self.db_path,
            "schema_version": self.schema_version,
            "db_size_bytes": self.db_size_bytes,
            "counts": self.counts,
            "findings": [asdict(f) for f in self.findings],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "ok": self.ok,
            "critical_count": len(self.critical),
            "warning_count": len(self.warnings),
            "info_count": len(self.info),
        }

    def to_ascii(self) -> str:
        """Render to a single-block ASCII report for CLI."""
        lines: list[str] = []
        bar = "═" * 64
        lines.append("╔" + bar + "╗")
        title = "  SIBYL MEMORY · LINT REPORT  "
        pad = (66 - len(title)) // 2
        lines.append("║" + " " * pad + title + " " * (66 - pad - len(title)) + "║")
        lines.append("╠" + bar + "╣")
        lines.append(f"║  tenant       │ {self.tenant_id[:46]:<46}║")
        lines.append(f"║  db path      │ {Path(self.db_path).name[:46]:<46}║")
        lines.append(f"║  schema v     │ {str(self.schema_version)[:46]:<46}║")
        size_kb = self.db_size_bytes / 1024
        lines.append(f"║  db size      │ {f'{size_kb:.1f} KB':<46}║")
        lines.append("╠" + bar + "╣")
        for k in sorted(self.counts):
            lines.append(f"║  {k:<13}│ {str(self.counts[k]):<46}║")
        lines.append("╠" + bar + "╣")
        if not self.findings:
            lines.append("║  no findings · memory looks clean" + " " * 30 + "║")
        else:
            for f in self.findings:
                sev_marker = {"critical": "✗", "warning": "⚠", "info": "i"}.get(f.severity, "·")
                hdr = f"  [{sev_marker} {f.severity}] {f.check}"
                lines.append(f"║{hdr[:64]:<64}║")
                msg = f"    {f.message}"
                # wrap at 62 cols
                while msg:
                    chunk, msg = msg[:62], msg[62:]
                    lines.append(f"║{chunk:<64}║")
                if f.recovery:
                    rec = f"    → {f.recovery}"
                    while rec:
                        chunk, rec = rec[:62], rec[62:]
                        lines.append(f"║{chunk:<64}║")
        lines.append("╠" + bar + "╣")
        summary = f"  {len(self.critical)} critical · {len(self.warnings)} warnings · {len(self.info)} info"
        lines.append(f"║{summary[:64]:<64}║")
        lines.append("╚" + bar + "╝")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# Linter: the actual checks
# ----------------------------------------------------------------------

class Linter:
    """Local memory linter. Stateless; safe to instantiate per call."""

    def __init__(
        self,
        storage: Storage,
        *,
        tenant_id: str = DEFAULT_TENANT,
        soft_cap_bytes: int = DEFAULT_SOFT_CAP_BYTES,
        stale_days: int = DEFAULT_STALE_DAYS,
        flag_recency_days: int = DEFAULT_FLAG_RECENCY_DAYS,
    ) -> None:
        self._storage = storage
        self._tenant_id = tenant_id
        self._soft_cap = soft_cap_bytes
        self._stale_days = stale_days
        self._flag_recency = flag_recency_days

    def run(self) -> LintReport:
        from .storage import _utc_now_iso
        started_at = _utc_now_iso()

        findings: list[Finding] = []
        counts: dict[str, int] = {}

        with self._storage.connection() as conn:
            # schema version
            schema_row = conn.execute(
                "SELECT MAX(version) AS v FROM sibyl_memory_schema_version"
            ).fetchone()
            schema_version = schema_row["v"] if schema_row else None

            if schema_version is None or schema_version < EXPECTED_SCHEMA_VERSION:
                findings.append(Finding(
                    check="schema-version",
                    severity="critical",
                    message=(
                        f"DB schema version is {schema_version}, expected "
                        f">= {EXPECTED_SCHEMA_VERSION}"
                    ),
                    recovery="Reopen the MemoryClient: schema migrations run on construction.",
                ))

            # Row counts
            for tname in (
                "entities", "state_documents", "journal_events",
                "reference_documents", "archived_entities", "flagged_actors",
                "skill_proposals", "learning_runs",
            ):
                try:
                    row = conn.execute(
                        f"SELECT COUNT(*) AS n FROM {tname} WHERE tenant_id = ?",
                        (self._tenant_id,),
                    ).fetchone()
                    counts[tname] = int(row["n"]) if row else 0
                except Exception:
                    counts[tname] = -1  # table missing: schema-version check catches it

            # ── JSON validity (defense-in-depth; CHECK constraints catch most)
            findings.extend(self._lint_json_bodies(conn))

            # ── duplicate entity names across categories
            dupes = conn.execute(
                "SELECT name, COUNT(DISTINCT category) AS c "
                "FROM entities WHERE tenant_id = ? GROUP BY name HAVING c > 1",
                (self._tenant_id,),
            ).fetchall()
            for row in dupes:
                findings.append(Finding(
                    check="duplicate-entity",
                    severity="warning",
                    message=f"entity name '{row['name']}' appears in {row['c']} categories",
                    recovery="Pick one canonical category and archive or rename the others.",
                    detail={"name": row["name"], "category_count": int(row["c"])},
                ))

            # ── empty reference documents
            empties = conn.execute(
                "SELECT doc_key FROM reference_documents "
                "WHERE tenant_id = ? AND (body IS NULL OR length(trim(body)) = 0)",
                (self._tenant_id,),
            ).fetchall()
            for row in empties:
                findings.append(Finding(
                    check="empty-reference",
                    severity="warning",
                    message=f"reference document '{row['doc_key']}' has empty body",
                    recovery="Either populate the body or delete the row.",
                    detail={"doc_key": row["doc_key"]},
                ))

            # ── stale entities
            cutoff = (
                _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=self._stale_days)
            ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            stale = conn.execute(
                "SELECT category, name, updated_at FROM entities "
                "WHERE tenant_id = ? AND updated_at < ? "
                "ORDER BY updated_at ASC LIMIT 25",
                (self._tenant_id, cutoff),
            ).fetchall()
            for row in stale:
                findings.append(Finding(
                    check="stale-entity",
                    severity="info",
                    message=(
                        f"entity {row['category']}/{row['name']} hasn't been "
                        f"updated since {row['updated_at']} "
                        f"(> {self._stale_days} days)"
                    ),
                    recovery=(
                        "Update the entity, archive it if no longer relevant, "
                        "or extend the staleness window."
                    ),
                    detail={
                        "category": row["category"],
                        "name": row["name"],
                        "updated_at": row["updated_at"],
                    },
                ))

            # ── journal entries with no useful payload
            empty_journal = conn.execute(
                "SELECT id, ts FROM journal_events "
                "WHERE tenant_id = ? "
                "AND evaluated IS NULL AND acted IS NULL "
                "AND forward IS NULL AND extra IS NULL "
                "ORDER BY ts DESC LIMIT 10",
                (self._tenant_id,),
            ).fetchall()
            for row in empty_journal:
                findings.append(Finding(
                    check="journal-without-acts",
                    severity="info",
                    message=f"journal event {row['id'][:12]}… at {row['ts']} has no payload",
                    recovery="Either populate the event or delete it.",
                    detail={"id": row["id"], "ts": row["ts"]},
                ))

            # ── DB size vs soft cap
            # CAP-1 (2026-06-25 pre-launch audit): WAL-inclusive sizing so the
            # lint cap warning matches the enforced footprint (memory.db alone
            # under-reports while writes sit in memory.db-wal).
            db_size = db_size_bytes(self._storage.db_path)
            if db_size >= 0.8 * self._soft_cap:
                pct = db_size / self._soft_cap
                severity = "critical" if db_size >= self._soft_cap else "warning"
                findings.append(Finding(
                    check="db-soft-cap",
                    severity=severity,
                    message=(
                        f"local DB is at {pct * 100:.1f}% of the {self._soft_cap // (1024 * 1024)} MB cap"
                    ),
                    recovery=(
                        "Archive stale entities, prune old journal events, "
                        "or upgrade to Stake / Cloud / Lifetime to remove the cap."
                    ),
                    detail={"db_size_bytes": db_size, "soft_cap_bytes": self._soft_cap},
                ))

            # ── FTS rowcount integrity
            try:
                ents = conn.execute(
                    "SELECT COUNT(*) AS n FROM entities WHERE tenant_id = ?",
                    (self._tenant_id,),
                ).fetchone()["n"]
                fts = conn.execute(
                    "SELECT COUNT(*) AS n FROM entities_fts WHERE tenant_id = ?",
                    (self._tenant_id,),
                ).fetchone()["n"]
                if ents != fts:
                    findings.append(Finding(
                        check="fts-rowcount-mismatch",
                        severity="warning",
                        message=(
                            f"entities table has {ents} rows but FTS5 index "
                            f"has {fts}: they should match"
                        ),
                        recovery="Rebuild FTS index: client.rebuild_fts() (planned).",
                        detail={"entities": ents, "fts": fts},
                    ))
            except Exception:
                pass  # missing fts table → caught by schema-version check

            # ── recent flagged actors (info-level surface)
            recent_cutoff = (
                _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=self._flag_recency)
            ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            try:
                flagged = conn.execute(
                    "SELECT identifier, flagged_at, reason FROM flagged_actors "
                    "WHERE tenant_id = ? AND flagged_at >= ? "
                    "ORDER BY flagged_at DESC LIMIT 5",
                    (self._tenant_id, recent_cutoff),
                ).fetchall()
                for row in flagged:
                    findings.append(Finding(
                        check="flagged-actors-fresh",
                        severity="info",
                        message=(
                            f"recent flagged actor: {row['identifier']} "
                            f"({row['reason'][:60] if row['reason'] else 'no reason given'})"
                        ),
                        recovery="Review the actor record; ensure downstream actions respect the flag.",
                        detail={
                            "identifier": row["identifier"],
                            "flagged_at": row["flagged_at"],
                        },
                    ))
            except Exception:
                pass

        completed_at = _utc_now_iso()

        return LintReport(
            tenant_id=self._tenant_id,
            db_path=str(self._storage.db_path),
            schema_version=schema_version,
            db_size_bytes=db_size_bytes(self._storage.db_path),  # CAP-1: WAL-inclusive
            counts=counts,
            findings=findings,
            started_at=started_at,
            completed_at=completed_at,
        )

    # ------------------------------------------------------------------
    # Internal. JSON validity probe
    # ------------------------------------------------------------------
    def _lint_json_bodies(self, conn: Any) -> list[Finding]:
        out: list[Finding] = []
        # entities.body must be JSON object/array
        bad_entities = conn.execute(
            "SELECT id, category, name FROM entities "
            "WHERE tenant_id = ? AND json_valid(body) = 0 LIMIT 10",
            (self._tenant_id,),
        ).fetchall()
        for row in bad_entities:
            out.append(Finding(
                check="invalid-json-entity",
                severity="critical",
                message=f"entity {row['category']}/{row['name']} has invalid JSON body",
                recovery="Delete or repair the row. SDK CHECK constraints should have prevented this.",
                detail={"id": row["id"]},
            ))

        bad_states = conn.execute(
            "SELECT document_key FROM state_documents "
            "WHERE tenant_id = ? AND json_valid(body) = 0 LIMIT 10",
            (self._tenant_id,),
        ).fetchall()
        for row in bad_states:
            out.append(Finding(
                check="invalid-json-state",
                severity="critical",
                message=f"state_document {row['document_key']} has invalid JSON body",
                recovery="Repair or delete the row.",
                detail={"document_key": row["document_key"]},
            ))

        # journal_events fields are nullable but if present must be valid JSON
        bad_journal = conn.execute(
            "SELECT id FROM journal_events WHERE tenant_id = ? "
            "AND ("
            "(evaluated IS NOT NULL AND json_valid(evaluated) = 0) OR "
            "(acted IS NOT NULL AND json_valid(acted) = 0) OR "
            "(forward IS NOT NULL AND json_valid(forward) = 0) OR "
            "(extra IS NOT NULL AND json_valid(extra) = 0)"
            ") LIMIT 10",
            (self._tenant_id,),
        ).fetchall()
        for row in bad_journal:
            out.append(Finding(
                check="invalid-json-journal",
                severity="critical",
                message=f"journal_event {row['id']} has invalid JSON in one of its fields",
                recovery="Repair or delete the row.",
                detail={"id": row["id"]},
            ))

        return out


# ----------------------------------------------------------------------
# Convenience module-level function (mirrors scripts/memory-lint.mjs UX)
# ----------------------------------------------------------------------

def lint(storage: Storage, *, tenant_id: str = DEFAULT_TENANT, **kwargs: Any) -> LintReport:
    """Convenience: run a default lint pass."""
    return Linter(storage, tenant_id=tenant_id, **kwargs).run()
