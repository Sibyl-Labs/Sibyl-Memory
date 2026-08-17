"""multi_record_search — multi-record (linked-record) retrieval.

Two-stage retrieve-then-verify search. A drop-in for a single client.search()
call on workflow / linked-record queries: queries whose answer spans several
related records (e.g. feedback + bug + journal, report + email, sheet + report).

Why it exists (tester Run15): flat single-pass FTS5 AND-of-tokens requires one
record to contain the whole query vocabulary, so a query that needs several
linked records returns only the single strongest match and misses the rest.

  Stage 1 RECALL   per-significant-token search, union the candidates, track
                   which query tokens each record matched.
  Stage 2 VERIFY   - abstain if any significant term has zero corpus support
                     (so "rejected" / "denied" / injection queries return []);
                   - on a terminal-state query, drop purely-preparatory records
                     (draft / triage / forecast), negation-aware;
                   - ANCHOR-FIRST (hybrid): keep a candidate that is in the
                     anchor's cluster (matches >= 1 anchor term, the rarest most
                     discriminating tokens) OR clears the high-coverage bar
                     ANCHOR_HYBRID_HI. A non-anchor, mid-coverage candidate is
                     cross-cluster pollution and is dropped. The pure strict
                     filter killed pollution but over-dropped natural-language
                     evidence that lacks the rare anchor; the hybrid keeps both;
                   - rank by IDF-weighted coverage with a tier tiebreaker
                     (content tiers before contentless journal), keep
                     >= COVERAGE_THRESHOLD.

Bench: baseline single-pass 4/10; recall-only multipass 3/10 (REGRESSES). The
prior retrieve-then-verify scored 10/10 at 24 records but only ~0.36 recall at
50-100 companies (tester Runs 16/17) because its selectivity cutoff was a corpus
fraction (round(0.15 * corpus_n)) that lost meaning at scale: past ~150 records
almost every term read as "selective," so cross-cluster records cleared the gate.
The anchor-first rewrite (this version) defines the anchor RELATIVE to the rarest
query term, so the precision gate is scale-invariant (tester Runs 24-29:
100/100 recall, 0 pollution at 100 companies / 1621 writes). Abstention and the
terminal/prep gates are preserved unchanged.

ANCHOR_HYBRID_HI was tuned on a real-data retrieval diagnostic (LongMemEval text
combined into one store): the pure anchor-only filter regressed natural-language
recall (gold evidence that lacks the rare anchor); HI=0.65 restores it while
keeping synthetic-workflow pollution at 0. Per-question (oracle) retrieval is not
regressed by this change (NEW >= OLD).

CAVEAT — COVERAGE_THRESHOLD, ANCHOR_BAND, ANCHOR_HYBRID_HI, and the prep/terminal
lexicon are defaults validated against the synthetic multi-cluster scale test
(tests/test_anchor_resolver_2026_06_06.py) + the LongMemEval retrieval diagnostic;
re-validate if corpus structure changes.

Uses only the public MemoryClient surface (search / list_entities), so it adds
no coupling to client internals.
"""
from __future__ import annotations
import json
import math
import re

_STOP = {"the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be",
         "to", "of", "in", "on", "at", "for", "with", "this", "that",
         "final", "current", "by"}

# --- df=0 abstention classifier (N1, 2026-08-16) ------------------------------
# The Stage-1 abstention (`if df[t] == 0: return []`) is the load-bearing
# precision gate that collapses injection / "rejected" queries to []. But it
# cannot tell a CONTENT-shaped zero-df token ("rejected", "nonexistenttokenzzzq")
# from a FUNCTION-shaped one ("kiedy", "when", "gdzie"): _STOP is 23 English
# words with no interrogatives, so a question-shaped query in Polish (or any
# language whose function words survive tokenization) had one zero-support
# function word abstain the WHOLE query — the default MCP path returned nothing
# for "kiedy jest inwentaryzacja". This lexical prior is consulted ONLY at the
# df=0 decision point (never at token admission or scoring of supported tokens):
# a df=0 token that is function-shaped is DROPPED (carried zero corpus signal by
# construction), a df=0 token that is content-shaped still hard-abstains. Defined
# locally (no client-internal import) to preserve the module's documented
# no-coupling contract; it mirrors client._SEARCH_STOPWORDS' interrogative/
# auxiliary surplus plus compact PL / DE / FR / ES / CZ sets.
_DF0_FUNCTION = frozenset({
    # English interrogatives + auxiliaries (the surplus over _STOP)
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "does", "did", "have", "has", "had", "will", "would", "should", "could",
    "there", "these", "those", "about", "than", "then", "here",
    # Polish (compact interrogative / copula / conjunction set)
    "kiedy", "gdzie", "jaki", "jaka", "jakie", "jakiej", "jakiego",
    "ktory", "ktora", "ktore", "który", "która", "które", "czy", "jest",
    "sa", "są", "byl", "był", "byla", "była", "bylo", "było", "bedzie",
    "będzie", "jak", "ile", "kto", "kogo", "komu", "czego", "czemu",
    "dlaczego", "gdy", "oraz", "albo", "ale", "dla", "przez", "przy",
    "mamy", "macie",
    # German
    "wann", "wer", "wie", "warum", "wieso", "welche", "welcher", "welches",
    "sind", "wird",
    # French
    "quand", "qui", "quoi", "comment", "pourquoi", "quel", "quelle",
    "quels", "quelles",
    # Spanish
    "cuando", "cuándo", "donde", "dónde", "quien", "quién", "cual", "cuál",
    "como", "cómo", "porque",
    # Czech
    "kdy", "kde", "kdo", "proc", "proč", "jaky", "jaký",
    # --- N1 hardening (2026-08-16): explicit inflected / modal coverage that a
    # length net must NOT stand in for (see _df0_droppable). English modals /
    # auxiliaries / pronouns >=3 chars, and the declined Polish pronoun / copula
    # paradigms + their ASCII de-diacritic twins (the PL eval corpora spell many
    # forms without diacritics). Additive only; none is a plausible content
    # discriminator (no ticker / codename / brand code appears here).
    "shall", "might", "must", "been", "being", "cannot", "not", "nor",
    "you", "your", "they", "them", "their", "his", "her", "him", "its",
    "she", "our",
    "będą", "beda", "będziemy", "bedziemy", "będziecie", "bedziecie",
    "będziesz", "bedziesz", "były", "byly", "byli", "byłem", "bylem",
    "byłam", "bylam",
    "którym", "ktorym", "których", "ktorych", "którego", "ktorego",
    "któremu", "ktoremu", "którą", "której", "ktorej", "którzy", "ktorzy",
    "jaką", "jakim", "jakich", "jakże", "jakze",
    "welchem", "welchen", "waren", "kann", "muss", "soll",
    # --- Finding B (2026-08-16 adversarial panel): natural PL/other-language
    # questions still zeroed on the DEFAULT MCP path because the lexicon missed
    # common INFLECTED function forms (the być paradigm is fusional, so a future/
    # present/past person the store never carries collapsed the whole query). This
    # widens the lexicon to the high-frequency function inventory. HARD RULE held:
    # every entry below is a genuine function word (interrogative / copula /
    # auxiliary / modal / conjunction / preposition / pronoun / determiner) that is
    # SAFE to drop when absent — nothing that could be a content/entity token. The
    # known collisions were deliberately EXCLUDED (PL 'bez'=lilac, 'ten'/'nas'/
    # 'nią'; EN/PL 'one'/'ten' numbers, 'mine'/'can'/'may'; DE 'die'/'war'/'man'/
    # 'hat'; FR 'car'/'par'/'son'/'ton'; ES 'son'/'con'/'sin'/'era'; CZ 'byt'). ASCII
    # de-diacritic twins are included because the PL eval corpora spell many forms
    # without diacritics.
    # Polish — być (copula) paradigm completion (present / future / past / cond.)
    "jestem", "jesteś", "jestes", "jesteśmy", "jestesmy", "jesteście", "jestescie",
    "będę", "bede",
    "byłeś", "byles", "byłaś", "bylas", "byliśmy", "bylismy", "byłyśmy", "bylysmy",
    "byliście", "byliscie", "byłyście", "bylyscie",
    "bym", "byś", "bys", "byśmy", "bysmy", "byście", "byscie",
    "byłby", "bylby", "byłaby", "bylaby", "byłoby", "byloby", "byliby", "bylyby",
    "byłbym", "bylbym",
    # Polish — mieć (auxiliary "have") present + modals / impersonals
    "mam", "masz", "mają", "maja",
    "może", "moze", "można", "mozna", "trzeba", "należy", "nalezy", "wolno",
    "musi", "muszę", "musze", "musimy", "musicie", "muszą", "musza",
    "powinien", "powinna", "powinno", "powinni",
    # Polish — interrogative / relative paradigm completion
    "kim", "czym", "jacy", "jakiemu", "jakimi", "którymi", "ktorymi",
    "czyj", "czyja", "czyje", "czyich", "czyim",
    "ilu", "iloma", "skąd", "skad", "dokąd", "dokad", "gdzież", "gdziez", "którędy", "ktoredy",
    # Polish — conjunctions / particles
    "lub", "ani", "bądź", "badz", "czyli", "także", "takze", "też", "tez",
    "więc", "wiec", "jednak", "natomiast", "ponieważ", "poniewaz", "gdyż", "gdyz",
    "aby", "żeby", "zeby", "ażeby", "azeby", "jeśli", "jesli", "jeżeli", "jezeli",
    "chociaż", "chociaz", "choć", "choc", "zatem", "toteż", "totez", "bowiem",
    "albowiem", "nie", "już", "juz", "jeszcze", "tylko", "również", "rowniez",
    "teraz", "tutaj", "tam", "wtedy", "właśnie", "wlasnie", "prawie", "bardzo",
    "zawsze", "nigdy",
    # Polish — prepositions
    "przed", "pod", "nad", "między", "miedzy", "poza", "podczas", "według",
    "wedlug", "wobec", "ponad", "wśród", "wsrod", "obok", "wokół", "wokol",
    "oprócz", "oprocz", "spośród", "sposrod", "sprzed", "znad", "spod", "poprzez",
    "wewnątrz", "wewnatrz", "naprzeciw", "względem", "wzgledem", "odnośnie", "odnosnie",
    # Polish — pronouns / possessives / demonstratives
    "ona", "ono", "oni", "jego", "jej", "ich", "jemu", "niego", "niej", "nim",
    "nimi", "nich", "mnie", "ciebie", "tobie", "sobie", "siebie", "się", "sie",
    "swój", "swoj", "swoje", "swoja", "swoich", "swojego",
    "mój", "moj", "moje", "moja", "twój", "twoj", "twoje", "twoja",
    "nasz", "nasze", "nasza", "wasz", "wasze",
    "tego", "temu", "tym", "tej", "tych", "tymi",
    "taki", "taka", "takie", "takich", "takim", "taką", "taka",
    # English — high-frequency prepositions / conjunctions / pronouns the set missed
    "before", "after", "above", "below", "over", "under", "into", "onto", "upon",
    "within", "without", "between", "among", "amongst", "during", "through",
    "throughout", "toward", "towards", "against", "because", "although", "though",
    "unless", "until", "till", "while", "whilst", "whether", "yours", "ours",
    "theirs", "myself", "yourself", "itself", "themselves", "herself", "himself",
    "ourselves", "yourselves", "whoever", "whatever", "whenever", "wherever",
    "whichever", "whomever", "however", "moreover", "therefore", "thus", "hence",
    "otherwise", "meanwhile", "nevertheless", "nonetheless", "anyone", "anything",
    "everyone", "everything", "someone", "somebody", "anybody", "everybody",
    "nobody", "nothing", "none", "both", "either", "neither", "such", "same",
    "another", "per", "via", "versus", "despite", "except", "besides", "beside",
    "beyond", "inside", "outside", "near", "unto",
    # German — obvious missing high-frequency function words
    "ist", "und", "oder", "aber", "nicht", "kein", "keine", "keinen", "keinem",
    "keiner", "haben", "habe", "hast", "hatte", "hatten", "werden", "werde",
    "wurde", "wurden", "worden", "sein", "seine", "seiner", "seinem", "seinen",
    "seines", "durch", "unter", "gegen", "ohne", "nach", "vor", "bei", "beim",
    "zum", "zur", "dem", "der", "das", "des", "dass", "weil", "wenn", "denn",
    "doch", "auch", "noch", "nur", "schon", "mehr", "sehr", "wohin", "woher",
    "hätte", "haette", "würde", "wuerde", "könnte", "koennte", "sollte", "wollte",
    "möchte", "moechte", "können", "koennen", "müssen", "muessen", "dürfen",
    "duerfen", "sollen", "wollen", "mögen", "moegen", "wir", "uns", "euch",
    "mich", "dich", "sich", "ihm", "ihn", "ihnen", "ihre", "ihrer", "ihrem",
    "ihren", "mein", "meine", "dein", "deine", "unser", "unsere", "diese",
    "dieser", "dieses", "diesem", "diesen", "jede", "jeder", "jedes",
    # French — obvious missing high-frequency function words
    "est", "sont", "être", "etre", "avoir", "avait", "avaient", "était", "etait",
    "étaient", "etaient", "dans", "pour", "avec", "sans", "sous", "sur", "vers",
    "chez", "entre", "parmi", "pendant", "depuis", "jusque", "jusqu", "mais",
    "donc", "ainsi", "alors", "aussi", "encore", "dont", "lequel", "laquelle",
    "lesquels", "lesquelles", "combien", "cela", "celui", "celle", "ceux",
    "celles", "cette", "cet", "ces", "une", "aux", "leur", "leurs", "mon", "mes",
    "tes", "nos", "vos", "ses", "notre", "votre", "très", "tres",
    # Spanish — obvious missing high-frequency function words
    "está", "esta", "están", "estan", "estoy", "estás", "estas", "estamos",
    "ser", "estar", "haber", "hay", "fue", "fueron", "eran", "para", "por",
    "sobre", "desde", "hasta", "hacia", "según", "segun", "durante", "mediante",
    "pero", "aunque", "cuánto", "cuanto", "cuántos", "cuantos", "cuánta",
    "cuanta", "este", "esto", "estos", "ese", "esa", "eso", "esos", "esas",
    "aquel", "aquella", "aquello", "aquellos", "sus", "mis", "tus", "nuestro",
    "nuestra", "nuestros", "vuestro", "del", "una", "unos", "unas", "que",
    "quienes", "cuáles", "cuales",
    # Czech — obvious missing high-frequency function words
    "jsou", "jsem", "jste", "jsme", "bude", "budou", "budu", "budeš", "budes",
    "není", "neni", "nejsou", "kolik", "kam", "odkud", "kudy", "pro", "přes",
    "pres", "podle", "během", "behem", "protože", "protoze", "nebo", "když",
    "kdyz", "jako", "ještě", "jeste", "ovšem", "ovsem", "avšak", "avsak", "tedy",
    "proto", "jelikož", "jelikoz", "abych", "kdyby", "jestli", "pokud",
})


def _df0_droppable(tok: str) -> bool:
    """True if a zero-df token is FUNCTION-shaped (safe to drop) rather than
    CONTENT-shaped (must still abstain). Consulted ONLY at the df=0 decision
    point, never at token admission or scoring of supported tokens.

    Lexicon-ONLY (N1 hardening, 2026-08-16). Membership in the curated
    _DF0_FUNCTION set is the SOLE test. The first N1 revision also dropped any
    <=4-char ASCII-alpha zero-df token, but that length net was not a
    function-vs-content signal: it swept in exactly the short discriminators an
    entity / company store is queried by (tickers, codenames, 3-4-letter names,
    brand codes: 'acme', 'acer', 'weth', 'usdc', 'aero', 'visa', 'ford', 'meta',
    'ikea', 'sol'), and for an ABSENT such term it silently dropped-then-collapsed
    the query into a cross-entity firehose instead of the honest abstention the
    caller asked for. It also reopened the CORE-6/MH-3 fanout by letting arbitrary
    short garbage tokens 'continue' past the df=0 early-abort. Length is not a
    proxy for function-vs-content; only the lexicon is. Unlisted function words
    (any language, any length) fall through to hard-abstain, which is the safe
    direction (over-abstain, never over-recall); widen the lexicon to cover them."""
    return tok in _DF0_FUNCTION


_TERMINAL_Q = {"final", "resolved", "approved", "published", "closed", "sent",
               "emailed", "decision", "finalized"}

_TERM_RE = re.compile(
    r'(?<!not )\b(final|finaliz\w*|resolved|approved|published|closed|sent|'
    r'emailed|decision|signed|bound)\b')
_PREP_RE = re.compile(
    r'\b(draft|triage|forecast|planning|proposed|tentative|pending|agenda|'
    r'scheduled|rehearsal|sample|option|wip|follow-?up)\b|work in progress')

# --- anchor-first resolver constants (see CAVEAT in the module docstring) ---
# Replaces the 24-record bench tuning (SELECTIVE_CUTOFF_FRAC = 0.15) that
# collapsed at scale. The anchor is defined RELATIVE to the rarest query term,
# so it is scale-invariant.
ANCHOR_BAND = 2.0              # a term is an "anchor" if df <= ANCHOR_BAND * rarest-term df
COVERAGE_THRESHOLD = 0.45      # hard coverage floor: drop candidates below this
ANCHOR_HYBRID_HI = 0.65        # a non-anchor candidate is kept only if coverage >= this
_PER_TOKEN_LIMIT = 200         # recall depth per token
# content tiers beat the contentless journal tier at equal coverage (cross-tier
# BM25 scores are not comparable; tester email 19e7eb3096b4dae5)
_TIER_PRIORITY = {"entity": 0, "state": 0, "reference": 0, "journal": 1}


def _significant_tokens(query: str):
    # v0.5.0 multi-language search (spec §4.1; absorbs PR #25's 0.4.20 fix).
    #
    # PR #25 diagnosis (0.4.20, Discord ticket 2026-08-04): the old ASCII-only
    # class ``[A-Za-z0-9]+`` shattered any word with a non-ASCII letter into
    # index-absent fragments ("Bełżyce" -> ['yce']) and produced NO tokens for
    # fully non-Latin scripts (Cyrillic/CJK/Greek/Arabic -> []); multi_record_search
    # abstains (``return []``) as soon as one token has df=0, so a single accented
    # word silently zeroed the whole cross-tier result. #25 moved to ``\w+``.
    #
    # This supersedes #25's one-line change with the SCRIPT-AWARE form, closing
    # three residual mechanisms #25's ``\w+`` still left broken (measured Stage A,
    # 87/100, zero ASCII behaviour change):
    #   M1 non-ASCII split  — ``\w`` keeps the accented/foreign word whole.
    #   M2 length filter    — the ``len(t) > 2`` floor is ASCII-calibrated: 2-char
    #                         CJK/Hangul words are the NORM and Brahmic combining
    #                         marks fragment to <=2 chars, so the floor dropped
    #                         every token -> abstain. It is applied to the ASCII
    #                         path ONLY; short non-ASCII tokens are kept.
    #   M3 case-fold order  — ``query.lower()`` BEFORE splitting changes length on
    #                         the U+0130 dotted-I class ('İstanbul'.lower() emits
    #                         i + U+0307), which ``\w+`` then splits. We split
    #                         FIRST and case-fold per token only when it is a safe
    #                         1:1 fold (len unchanged); otherwise keep the raw
    #                         token (FTS5 does its own case folding downstream).
    #
    # ASCII invariant: pure-ASCII queries produce the EXACT 0.4.19 token stream
    # (stopword drop + len>2 + lower). Guarded by
    # test_unicode_query_tokens_2026_08_04.py (#25) and
    # test_script_aware_tokens_2026_08_06.py.
    toks = []
    for t in re.findall(r"\w+", query):          # split BEFORE case-folding (M3)
        if t.isascii():
            t = t.lower()
            if len(t) > 2 and t not in _STOP:    # ASCII path: UNCHANGED semantics
                toks.append(t)
        else:
            low = t.lower()
            # Case-fold only when it is a safe 1:1 fold (e.g. Cyrillic, Greek);
            # keep the raw token where folding changes length (U+0130 dotted-I).
            # FTS5 does its own case folding, so the raw token is always safe to
            # pass. Short non-ASCII tokens (2-char CJK words, Brahmic fragments)
            # are REAL units and are kept (no len>2 filter here — M2).
            toks.append(low if len(low) == len(t) else t)
    return toks


# CORE-6/MH-3 (2026-06-25 pre-launch audit): cap the per-token recall fan-out.
# An attacker (or a pathological query) with many significant tokens previously
# issued one 200-row FTS5 search PER token, an unbounded multiplier on a single
# untiered call. Bound the fan-out to the most-significant (longest, a cheap
# rarity proxy) tokens so the work per query is O(MAX_FANOUT_TOKENS), not
# O(len(query)).
_MAX_FANOUT_TOKENS = 24


def _corpus_count(client) -> int:
    """Cheap corpus size for IDF weighting (CORE-6/MH-3).

    Prefer the client's storage COUNT(*) over the old
    ``len(list_entities(limit=100000))``, which materialized + JSON-decoded every
    entity row just to count them. Falls back to the old path only if the cheap
    method is unavailable (older client without count_rows / storage access).
    """
    storage = getattr(client, "storage", None)
    tenant = None
    get_tenant = getattr(client, "get_tenant", None)
    if callable(get_tenant):
        try:
            tenant = get_tenant()
        except Exception:
            tenant = None
    if storage is not None and tenant is not None and hasattr(storage, "count_rows"):
        try:
            return storage.count_rows("entities", tenant)
        except Exception:
            pass
    # Fallback: bounded list (still cheaper than the old 100000 with the clamp).
    return len(client.list_entities(limit=10_000))


def _pure_prep(body_lower: str) -> bool:
    """True if the body is purely preparatory (a prep marker, no terminal marker)."""
    return bool(_PREP_RE.search(body_lower)) and not bool(_TERM_RE.search(body_lower))


def multi_record_search(client, query: str, *, limit: int = 10, corpus_n: int | None = None):
    """Two-stage retrieve-then-verify search over a MemoryClient.

    Returns a ranked list of hit dicts in the SAME shape client.search() returns
    ({tier, key, category, body, snippet, rank, ts}), best-first. Returns [] when
    the query is unsatisfiable (abstention) or nothing clears the verify gates.

    For exact single-entity lookups, prefer client.recall() / get_entity().
    """
    toks = _significant_tokens(query)
    if not toks:
        return []
    # CORE-6/MH-3: bound token fan-out. De-dup, then keep the longest (rarest-
    # proxy) tokens up to the cap so an attacker can't force one FTS5 search per
    # token on an arbitrarily long query. Terminal-state keywords are always
    # retained so the terminal/prep gate still has its signal.
    uniq = list(dict.fromkeys(toks))
    if len(uniq) > _MAX_FANOUT_TOKENS:
        forced = [t for t in uniq if t in _TERMINAL_Q]
        rest = sorted((t for t in uniq if t not in _TERMINAL_Q), key=len, reverse=True)
        keep = list(dict.fromkeys(forced + rest))[:_MAX_FANOUT_TOKENS]
        toks = keep
    else:
        toks = uniq
    if corpus_n is None:
        corpus_n = _corpus_count(client)  # CORE-6/MH-3: cheap COUNT(*), not full scan

    terminal_q = bool(set(toks) & _TERMINAL_Q)

    cand: dict = {}
    df: dict = {}
    for t in toks:
        hits = client.search(t, limit=_PER_TOKEN_LIMIT)
        df[t] = len(hits)
        if df[t] == 0:
            # N1: abstain only on a CONTENT-shaped zero-df term (the injection /
            # "rejected" class). A FUNCTION-shaped zero-df token ("kiedy",
            # "when", "gdzie") carried no corpus signal by construction, so it is
            # dropped after the loop instead of collapsing the whole query.
            if not _df0_droppable(t):
                return []  # abstention: a discriminating term nothing satisfies
            continue  # accumulate no candidates for a droppable zero-df token
        for h in hits:
            key = (h.get("tier"), h.get("key"), h.get("category"))
            e = cand.get(key)
            if e is None:
                # CORE-6/MH-3: only serialize+lower the body when a terminal-state
                # query will actually consult it (the prep/terminal gate). For
                # non-terminal queries the body string is never read, so skip the
                # per-hit json.dumps entirely.
                body_lower = json.dumps(h.get("body")).lower() if terminal_q else ""
                e = cand[key] = {"m": set(), "best": 0.0, "hit": h, "body": body_lower}
            e["m"].add(t)
            rank = h.get("rank", 0.0) or 0.0
            if rank < e["best"]:
                e["best"] = rank

    # N1: drop the FUNCTION-shaped zero-df tokens BEFORE idf / min_df / anchor_cut.
    # Filtering df here is load-bearing: it makes the coverage denominator exclude
    # dropped tokens (so 'kiedy jest inwentaryzacja' scores coverage 1.0 on
    # 'inwentaryzacja') and prevents a min_df=0 from poisoning the anchor band.
    # terminal_q was computed from the PRE-drop toks (above), so a dropped zero-df
    # 'sent' still keeps the terminal/prep gate armed.
    if any(df[t] == 0 for t in toks):
        toks = [t for t in toks if df[t] > 0]
        if not toks:
            return []
        df = {t: df[t] for t in toks}

    idf = {t: math.log((corpus_n + 1) / (df[t] + 1)) + 1.0 for t in toks}
    total = sum(idf.values()) or 1.0

    # Anchor-first: anchor terms are the rarest (most discriminating) tokens,
    # defined relative to the rarest term so the band is scale-invariant. Every
    # candidate is strict-filtered to the anchor's cluster (must match >= 1 anchor
    # term), which removes the cross-cluster pollution the old corpus-fraction
    # cutoff let through at scale. Anchor-raw recalls fully but pollutes; the
    # strict filter is the load-bearing precision gate (tester Runs 24-29).
    min_df = min(df.values())
    anchor_cut = max(2, round(ANCHOR_BAND * min_df))
    anchor_terms = {t for t in toks if df[t] <= anchor_cut}

    scored = []
    for e in cand.values():
        if terminal_q and _pure_prep(e["body"]):
            continue                                   # drop purely-preparatory on a final-state query
        cov = sum(idf[t] for t in e["m"]) / total
        if cov < COVERAGE_THRESHOLD:
            continue                                   # below the hard coverage floor
        # Anchor-first HYBRID gate: keep a candidate that is in the anchor's
        # cluster (matches an anchor term) OR clears the high-coverage bar
        # (genuinely relevant despite lacking the rare anchor, e.g. natural-
        # language evidence). A non-anchor, mid-coverage candidate is pure
        # cross-cluster pollution and is dropped. Tuned on the LongMemEval
        # retrieval diagnostic: synthetic-workflow pollution -> 0 while natural-
        # language recall is preserved (anchor-only over-filtered real queries).
        if anchor_terms and not (e["m"] & anchor_terms) and cov < ANCHOR_HYBRID_HI:
            continue
        tier = e["hit"].get("tier")
        scored.append((e["hit"], cov, _TIER_PRIORITY.get(tier, 0), e["best"]))
    scored.sort(key=lambda x: (-x[1], x[2], x[3]))
    return [h for h, _cov, _tp, _best in scored[:limit]]
