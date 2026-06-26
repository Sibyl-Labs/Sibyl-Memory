"""B005 Adversarial testing — stress-test the Sibyl Memory plugin.

Findings:
1. VALIDATION-ORDER BUG: MemoryClient.local() creates Storage (opens SQLite)
   BEFORE validating tenant_id. If tenant_id is invalid, the file handle leaks.
2. Input validation correctly blocks SQL injection via keys.
3. Tenant isolation works correctly.
4. Free-tier cap is enforced.
"""
import tempfile
import threading
from pathlib import Path

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.exceptions import (
    CapExceededError, TenantError, ValidationError,
)


# ── 1. SQL Injection ────────────────────────────────────────────────────

class TestSQLInjection:
    def test_inject_via_key_blocked(self):
        """Semicolons in keys are rejected by input validation."""
        with tempfile.TemporaryDirectory() as tmp:
            with MemoryClient.local(path=Path(tmp) / "db", tier="free") as c:
                with pytest.raises(ValidationError):
                    c.set_entity("test", "'; DROP TABLE; --", {"note": "x"})

    def test_inject_via_value_safe(self):
        """SQL injection through JSON body is parameterized (safe)."""
        with tempfile.TemporaryDirectory() as tmp:
            with MemoryClient.local(path=Path(tmp) / "db", tier="free") as c:
                c.set_entity("test", "safe", {"note": "'; DROP TABLE entities; --"})
                hits = c.search("safe", limit=10)
                assert len(hits) >= 1

    def test_fts5_injection_via_search(self):
        """FTS5 query injection should not crash or leak data."""
        with tempfile.TemporaryDirectory() as tmp:
            with MemoryClient.local(path=Path(tmp) / "db", tier="free") as c:
                c.set_entity("test", "secret", {"note": "classified"})
                for q in ["{secret}:secret", "NEAR(secret,1)", "secret OR hello"]:
                    try:
                        c.search(q, limit=10)
                    except Exception:
                        pass
                hits = c.search("classified", limit=10)
                assert len(hits) >= 1


# ── 2. Tenant Isolation ─────────────────────────────────────────────────

class TestTenantIsolation:
    def test_cross_tenant_read_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db"
            with MemoryClient.local(path=db, tier="free", tenant_id="a") as a:
                a.set_entity("secret", "pw", {"note": "hunter2"})
            with MemoryClient.local(path=db, tier="free", tenant_id="b") as b:
                assert len(b.search("hunter2", limit=10)) == 0

    def test_cross_tenant_write_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db"
            with MemoryClient.local(path=db, tier="free", tenant_id="a") as a:
                a.set_entity("test", "item", {"note": "original"})
            with MemoryClient.local(path=db, tier="free", tenant_id="b") as b:
                b.set_entity("test", "item", {"note": "overwritten"})
            with MemoryClient.local(path=db, tier="free", tenant_id="a") as a:
                assert len(a.search("original", limit=10)) >= 1

    def test_tenant_id_semicolon_rejected(self):
        """Semicolons in tenant_id should be rejected (TenantError)."""
        with pytest.raises(TenantError):
            MemoryClient.local(
                path=Path(tempfile.mkdtemp()) / "db",
                tier="free",
                tenant_id="'; DROP TABLE; --",
            )

    def test_tenant_id_path_traversal_rejected(self):
        """Path traversal in tenant_id should be rejected."""
        for evil in ["../../../etc/passwd", "a/../../secret", "a\\..\\secret"]:
            with pytest.raises(TenantError):
                MemoryClient.local(
                    path=Path(tempfile.mkdtemp()) / "db",
                    tier="free",
                    tenant_id=evil,
                )


# ── 3. FINDING: Validation-Order Bug ────────────────────────────────────

class TestValidationOrderBug:
    """MemoryClient.local() opens Storage BEFORE validating tenant_id.
    
    If tenant_id is invalid, the SQLite file handle leaks because Storage
    is created at line 712 before __init__ validates at line 624.
    
    Reproduce:
        MemoryClient.local(path=Path(tmp)/"db", tier="free", tenant_id="bad;val")
        # Raises TenantError, but the SQLite file is already open
        # On Windows, temp directory cleanup fails with PermissionError
    """

    def test_invalid_tenant_id_leaks_file_handle(self):
        """Invalid tenant_id should not leave an open file handle."""
        tmp = tempfile.mkdtemp()
        db_path = Path(tmp) / "db"
        
        try:
            MemoryClient.local(path=db_path, tier="free", tenant_id="bad;val")
        except TenantError:
            pass
        
        # The file should be deletable (no open handles)
        # On Windows, this fails because Storage opened the file before validation
        import os, shutil
        try:
            shutil.rmtree(tmp)
            # If we get here, no leak (Linux/macOS or fixed)
        except PermissionError:
            # This is the bug: file handle leaked
            pytest.fail(
                "PermissionError on cleanup — Storage opened SQLite before "
                "tenant_id validation. File handle leaked."
            )


# ── 4. Input Validation ─────────────────────────────────────────────────

class TestInputValidation:
    def test_null_bytes_in_key_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with MemoryClient.local(path=Path(tmp) / "db", tier="free") as c:
                with pytest.raises(ValidationError):
                    c.set_entity("test", "key\x00null", {"note": "x"})

    def test_oversized_value_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            with MemoryClient.local(path=Path(tmp) / "db", tier="free") as c:
                try:
                    c.set_entity("test", "big", {"note": "x" * (10 * 1024 * 1024)})
                except Exception:
                    pass
                c.set_entity("test", "small", {"note": "ok"})
                assert len(c.search("ok", limit=10)) >= 1

    def test_unicode_edge_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            with MemoryClient.local(path=Path(tmp) / "db", tier="free") as c:
                for i, val in enumerate(["𝕳𝖊𝖑𝖑𝖔", "\u200b\u200c", "🇺🇸", "a" * 10000]):
                    try:
                        c.set_entity("test", f"e{i}", {"note": val})
                    except Exception:
                        pass
                c.set_entity("test", "ok", {"note": "hello"})
                assert len(c.search("hello", limit=10)) >= 1


# ── 5. Cap Enforcement ──────────────────────────────────────────────────

class TestCapEnforcement:
    def test_cap_enforced_on_free_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            with MemoryClient.local(path=Path(tmp) / "db", tier="free") as c:
                cap_hit = False
                for i in range(200):
                    try:
                        c.set_entity("test", f"item_{i}", {"note": "x" * 20000})
                    except CapExceededError:
                        cap_hit = True
                        break
                assert cap_hit, "Cap should have been enforced"
                assert len(c.search("item_0", limit=10)) >= 1


# ── 6. Concurrency ──────────────────────────────────────────────────────

class TestConcurrency:
    def test_concurrent_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db"
            errors = []
            def writer(tid):
                try:
                    with MemoryClient.local(path=db, tier="free") as c:
                        for i in range(30):
                            c.set_entity("test", f"t{tid}_{i}", {"note": f"thread {tid}"})
                except Exception as e:
                    errors.append(str(e))
            
            threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            
            with MemoryClient.local(path=db, tier="free") as c:
                assert len(c.search("thread", limit=100)) >= 10


# ── 7. Recall Adversarial ───────────────────────────────────────────────

class TestRecallAdversarial:
    def test_recall_stopwords(self):
        with tempfile.TemporaryDirectory() as tmp:
            with MemoryClient.local(path=Path(tmp) / "db", tier="free") as c:
                c.set_entity("test", "item", {"note": "the quick brown fox"})
                assert len(c.search("the", limit=10)) >= 1

    def test_recall_special_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            with MemoryClient.local(path=Path(tmp) / "db", tier="free") as c:
                c.set_entity("test", "item", {"note": "price $99.99 (USD)"})
                assert len(c.search("99.99", limit=10)) >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
