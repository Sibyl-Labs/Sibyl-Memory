"""Permanent in-repo multi-language coverage guard (v0.5.0, spec §7).

A trimmed, self-contained slice of the 100-language sandbox harness so CI holds
the line WITHOUT the external sandbox. Each row: write a native-script entity,
then search a genuine native-script token via ``multi_record_search`` — the exact
default path a real MCP caller hits (server.py untiered ``memory_search`` ->
``multi_record_search``). One native-script write+query probe per language; this
is a regression tripwire, NOT a claim of full linguistic search quality (no word
segmentation, no romanization/cross-script, no non-English stemming — see the
spec §7 honest-scope note).

Baseline before this patch: 21/100. These 13 rows span the mechanisms the patch
fixes — CJK/Japanese/Thai/Zulu substring glue (M4), Hangul + Brahmic short
tokens (M2), Turkish dotted-I (M3), Polish ł fold (M5), and the space-delimited
non-Latin scripts (M1: Cyrillic/Greek/Arabic).
"""
from __future__ import annotations

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.multi_record import multi_record_search


# (iso, language, native content written, native query token)
SMOKE_LANGS = [
    ("zh", "Chinese, Mandarin", "北京烤鸭", "北京"),        # M4 CJK glue
    ("ja", "Japanese", "東京タワー", "東京"),                # M4 CJK glue
    ("ko", "Korean", "서울 도시", "서울"),                   # M2 2-char Hangul
    ("hi", "Hindi", "दिल्ली शहर", "दिल्ली"),                # M2 Devanagari fragments
    ("bn", "Bengali", "ঢাকা শহর", "ঢাকা"),                 # M2 Bengali fragments
    ("ta", "Tamil", "சென்னை நகரம்", "சென்னை"),            # M2 Tamil fragments
    ("th", "Thai", "เมืองเชียงใหม่", "เชียงใหม่"),          # M4 Thai glue
    ("ar", "Arabic", "القاهرة مدينة", "القاهرة"),          # M1 Arabic (primary index)
    ("ru", "Russian", "Москва город", "Москва"),           # M1 Cyrillic
    ("el", "Greek", "Αθήνα πόλη", "Αθήνα"),                # M1 Greek
    ("pl", "Polish", "Bełżyce miasto", "Bełżyce"),         # M5 ł fold class
    ("tr", "Turkish", "İstanbul şehri", "İstanbul"),       # M3 dotted-I case fold
    ("zu", "Zulu", "Idolobha laseThekwini", "Thekwini"),   # M4 Bantu locative glue
]


@pytest.fixture(scope="module")
def smoke_client(tmp_path_factory):
    path = tmp_path_factory.mktemp("lang-smoke") / "m.db"
    c = MemoryClient.local(path, tenant_id="lang-smoke")
    for iso, language, content, _query in SMOKE_LANGS:
        c.set_entity("lang_test", f"lang_{iso}",
                     {"text": content, "language": language, "iso": iso})
    return c


@pytest.mark.parametrize("iso, language, content, query",
                         SMOKE_LANGS, ids=[r[0] for r in SMOKE_LANGS])
def test_native_script_search_finds_record(smoke_client, iso, language, content, query):
    hits = multi_record_search(smoke_client, query, limit=20)
    keys = {(h.get("tier"), h.get("category"), h.get("key")) for h in hits}
    assert ("entity", "lang_test", f"lang_{iso}") in keys, (
        f"{language} ({iso}) query {query!r} did not surface its record; got {keys}")


def test_smoke_baseline_all_pass(smoke_client):
    """The aggregate tripwire: every smoke language must pass (13/13)."""
    passed = 0
    for iso, _language, _content, query in SMOKE_LANGS:
        hits = multi_record_search(smoke_client, query, limit=20)
        if any(h.get("key") == f"lang_{iso}" for h in hits):
            passed += 1
    assert passed == len(SMOKE_LANGS), f"{passed}/{len(SMOKE_LANGS)} smoke languages passed"
