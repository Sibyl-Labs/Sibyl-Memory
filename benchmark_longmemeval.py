"""LongMemEval benchmark for Sibyl Memory Plugin (B003).

Runs a simplified recall benchmark: loads haystack sessions into Sibyl Memory,
searches for the answer, and checks if the relevant information is retrievable.

Methodology:
- For each of 500 LongMemEval Oracle questions:
  1. Create a fresh Sibyl Memory instance
  2. Load all haystack sessions as memory entries
  3. Search for the answer text
  4. Check if any search result contains the answer
- Report accuracy per question type and overall

This tests memory RETRIEVAL, not generation. A full benchmark would use an LLM
to generate answers from retrieved context, but this isolates the memory layer.
"""
import json
import sys
import tempfile
from pathlib import Path
from collections import Counter

# Add sibyl-memory-client to path
sys.path.insert(0, str(Path(__file__).parent / "sibyl-memory-client" / "src"))

from sibyl_memory_client import MemoryClient


def load_dataset():
    """Load LongMemEval Oracle dataset from Hugging Face."""
    from huggingface_hub import hf_hub_download
    path = hf_hub_download(
        'xiaowu0162/longmemeval-cleaned',
        'longmemeval_oracle.json',
        repo_type='dataset',
    )
    with open(path) as f:
        return json.load(f)


def load_sessions_into_memory(client: MemoryClient, sessions: list, question_id: str):
    """Load haystack sessions into Sibyl Memory as conversation entries."""
    for i, session in enumerate(sessions):
        if isinstance(session, list):
            # Session is a list of messages
            for j, msg in enumerate(session):
                if isinstance(msg, dict):
                    role = msg.get('role', 'user')
                    content = msg.get('content', '')
                    if content:
                        client.set_entity(
                            "conversation",
                            f"{question_id}_s{i}_m{j}",
                            {
                                "role": role,
                                "content": content,
                                "session_idx": i,
                                "msg_idx": j,
                            },
                        )
        elif isinstance(session, str):
            # Session is a raw string
            if session.strip():
                client.set_entity(
                    "conversation",
                    f"{question_id}_s{i}",
                    {"content": session.strip(), "session_idx": i},
                )


def check_answer_in_results(answer: str, hits: list) -> bool:
    """Check if the answer (or key parts of it) appear in search results."""
    answer_lower = answer.lower().strip()
    
    for hit in hits:
        # Check all string fields in the hit
        for key, val in hit.items():
            if isinstance(val, str) and answer_lower in val.lower():
                return True
            if isinstance(val, dict):
                for v in val.values():
                    if isinstance(v, str) and answer_lower in v.lower():
                        return True
    
    # Also try token-level matching (answer words appear in results)
    answer_tokens = set(answer_lower.split())
    if len(answer_tokens) <= 2:
        return False  # Too short for token matching
    
    for hit in hits:
        hit_text = json.dumps(hit).lower()
        matched = sum(1 for t in answer_tokens if t in hit_text)
        if matched >= len(answer_tokens) * 0.7:  # 70% token overlap
            return True
    
    return False


def run_benchmark(limit: int = None, verbose: bool = False):
    """Run the LongMemEval benchmark."""
    print("Loading LongMemEval Oracle dataset...")
    data = load_dataset()
    
    if limit:
        data = data[:limit]
    
    print(f"Running benchmark on {len(data)} questions...")
    print()
    
    results = {
        'total': 0,
        'correct': 0,
        'by_type': Counter(),
        'correct_by_type': Counter(),
        'errors': [],
    }
    
    for i, q in enumerate(data):
        qid = q['question_id']
        qtype = q['question_type']
        question = q['question']
        answer = q['answer']
        sessions = q['haystack_sessions']
        
        results['total'] += 1
        results['by_type'][qtype] += 1
        
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "memory.db"
                
                with MemoryClient.local(path=db_path, tier="staker") as client:
                    # Load sessions into memory
                    load_sessions_into_memory(client, sessions, qid)
                    
                    # Search for the answer
                    # Try searching with key terms from the answer
                    answer_terms = answer.split()[:3]  # First 3 words
                    query = ' '.join(answer_terms)
                    
                    hits = client.search(query, limit=10)
                    
                    found = check_answer_in_results(answer, hits)
                    
                    if found:
                        results['correct'] += 1
                        results['correct_by_type'][qtype] += 1
                    
                    if verbose and not found:
                        results['errors'].append({
                            'id': qid,
                            'type': qtype,
                            'question': question[:100],
                            'answer': answer[:100],
                            'query': query,
                            'hits': len(hits),
                        })
        
        except Exception as e:
            if verbose:
                results['errors'].append({
                    'id': qid,
                    'type': qtype,
                    'error': str(e)[:200],
                })
        
        # Progress
        if (i + 1) % 50 == 0:
            acc = results['correct'] / results['total'] * 100
            print(f"  [{i+1}/{len(data)}] Accuracy so far: {acc:.1f}%")
    
    # Final report
    print()
    print("=" * 60)
    print("LongMemEval Oracle Benchmark — Sibyl Memory Plugin")
    print("=" * 60)
    print()
    
    overall_acc = results['correct'] / results['total'] * 100
    print(f"Overall Accuracy: {results['correct']}/{results['total']} = {overall_acc:.1f}%")
    print()
    print("By Question Type:")
    print(f"  {'Type':<30} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
    print(f"  {'-'*30} {'-'*8} {'-'*8} {'-'*10}")
    
    for qtype in sorted(results['by_type'].keys()):
        total = results['by_type'][qtype]
        correct = results['correct_by_type'].get(qtype, 0)
        acc = correct / total * 100 if total > 0 else 0
        print(f"  {qtype:<30} {correct:>8} {total:>8} {acc:>9.1f}%")
    
    print()
    print("Methodology:")
    print("  - Dataset: LongMemEval Oracle (500 questions, 6 types)")
    print("  - Memory: Sibyl Memory Plugin (sibyl-memory-client 0.4.15)")
    print("  - Tier: staker (full access)")
    print("  - Approach: Load sessions → search for answer terms → check recall")
    print("  - Match: Exact substring + 70% token overlap")
    print("  - This tests MEMORY RETRIEVAL, not LLM generation")
    print()
    print("Comparison with Sibyl's published results:")
    print("  - Sibyl claims 95.6% with Claude Opus 4.6 (full LLM pipeline)")
    print("  - This benchmark isolates the memory layer only")
    print("  - Lower scores are expected (no LLM reasoning)")
    
    if results['errors'] and verbose:
        print()
        print(f"Sample failures ({min(5, len(results['errors']))} shown):")
        for err in results['errors'][:5]:
            print(f"  {err.get('id', '?')}: {err.get('question', err.get('error', '?'))[:80]}")
    
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="LongMemEval benchmark for Sibyl Memory")
    parser.add_argument("--limit", type=int, help="Limit number of questions")
    parser.add_argument("--verbose", action="store_true", help="Show failures")
    args = parser.parse_args()
    
    run_benchmark(limit=args.limit, verbose=args.verbose)
