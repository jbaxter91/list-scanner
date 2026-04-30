from __future__ import annotations

import argparse
import random
import re
import statistics
import string
import time

import main


def _safe_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _norm(s: str) -> str:
    return s.lower().strip().strip('.,;:!?"\'-')


def locate_legacy(search: str, data: dict, scale: int = 1) -> list[tuple]:
    """
    Baseline implementation copied from pre-optimization main._locate.
    Kept here to validate parity and measure speedup.
    """
    words = data["text"]
    confs = data["conf"]
    query = [_norm(w) for w in search.split() if w.strip()]
    if not query:
        return []

    valid = [
        i for i, (w, c) in enumerate(zip(words, confs))
        if w.strip() and _safe_int(c) >= 30
    ]

    sep_query = [_norm(w) for w in re.split(r"[.\-_]+", search) if w.strip()]

    def _scan_tokens(q_words: list[str]) -> list[tuple]:
        m = len(q_words)
        hits = []
        if m == 1:
            q = q_words[0]
            for idx in valid:
                if q in _norm(words[idx]):
                    hits.append((
                        data["left"][idx] // scale,
                        data["top"][idx] // scale,
                        data["width"][idx] // scale,
                        data["height"][idx] // scale,
                    ))
        else:
            for k in range(len(valid) - m + 1):
                chunk = [_norm(words[valid[k + j]]) for j in range(m)]
                if all(qw in c for qw, c in zip(q_words, chunk)):
                    ids = [valid[k + j] for j in range(m)]
                    x0 = min(data["left"][i] for i in ids)
                    y0 = min(data["top"][i] for i in ids)
                    x1 = max(data["left"][i] + data["width"][i] for i in ids)
                    y1 = max(data["top"][i] + data["height"][i] for i in ids)
                    hits.append((x0 // scale, y0 // scale,
                                 (x1 - x0) // scale, (y1 - y0) // scale))
        return hits

    results = _scan_tokens(query)
    if not results and sep_query != query:
        results = _scan_tokens(sep_query)
    return results


def _random_word(rng: random.Random, lo: int = 4, hi: int = 11) -> str:
    n = rng.randint(lo, hi)
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(n))


def _build_fake_ocr(rng: random.Random, token_count: int) -> dict:
    text = []
    conf = []
    left = []
    top = []
    width = []
    height = []

    cursor_x = 0
    cursor_y = 0
    line_h = 24
    for i in range(token_count):
        if i % 20 == 0:
            cursor_x = 0
            cursor_y += line_h

        w = _random_word(rng)
        # Inject punctuation-separated tokens to exercise fallback query path.
        if i % 37 == 0:
            w = f"{_random_word(rng, 3, 6)}.{_random_word(rng, 3, 6)}"
        text.append(w)
        conf.append(str(rng.randint(30, 96)))

        box_w = rng.randint(30, 95)
        box_h = rng.randint(14, 22)
        left.append(cursor_x)
        top.append(cursor_y)
        width.append(box_w)
        height.append(box_h)

        cursor_x += box_w + rng.randint(4, 16)

    return {
        "text": text,
        "conf": conf,
        "left": left,
        "top": top,
        "width": width,
        "height": height,
    }


def _build_queries(rng: random.Random, data: dict, query_count: int) -> list[str]:
    words = [w for w in data["text"] if w.strip()]
    queries = []
    for i in range(query_count):
        if i % 5 == 0 and len(words) >= 2:
            j = rng.randint(0, len(words) - 2)
            queries.append(f"{words[j]} {words[j + 1]}")
        else:
            j = rng.randint(0, len(words) - 1)
            queries.append(words[j])

    # Add a few misses so both hit/miss logic is represented.
    queries.extend(["zzzzqwerty", "not-in-data-token", "alpha.beta.gamma"])
    return queries


def _time_legacy(queries: list[str], data: dict, scale: int, iterations: int) -> list[float]:
    """Legacy: one full _locate call (including index rebuild) per list item per pass."""
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        for q in queries:
            locate_legacy(q, data, scale)
        times.append((time.perf_counter() - t0) * 1000.0)
    return times


def _time_prepared(queries: list[str], data: dict, scale: int, iterations: int) -> list[float]:
    """Optimised: prepare index once per pass, then match each item."""
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        prepared = main._prepare_ocr_index(data, scale)
        for q in queries:
            main._locate_prepared(q, prepared)
        times.append((time.perf_counter() - t0) * 1000.0)
    return times


def _assert_parity(queries: list[str], data: dict, scale: int):
    prepared = main._prepare_ocr_index(data, scale)
    for q in queries:
        a = locate_legacy(q, data, scale)
        b = main._locate_prepared(q, prepared)
        if a != b:
            raise AssertionError(f"Parity mismatch for query={q!r}: legacy={a} optimised={b}")


def run_benchmark(seed: int, tokens: int, queries_n: int, iterations: int, scale: int):
    """
    Compares legacy (per-item index rebuild) vs optimised (prepare-once) at several
    realistic item counts so the result is meaningful for actual usage.
    """
    rng = random.Random(seed)
    data = _build_fake_ocr(rng, token_count=tokens)
    # Build a full pool then slice down per item-count scenario
    all_queries = _build_queries(rng, data, query_count=max(queries_n, 600))
    _assert_parity(all_queries[:50], data, scale)

    print("OCR locate benchmark  (parity=OK)")
    print(f"seed={seed}  ocr_tokens={tokens}  iterations={iterations}  scale={scale}")
    print(f"{'items':>6}  {'legacy_avg':>12}  {'optimised_avg':>14}  {'speedup':>8}  {'legacy_p95':>11}  {'opt_p95':>9}")
    print("-" * 72)

    for item_count in [5, 10, 20, 50, 100, queries_n]:
        queries = all_queries[:item_count]
        leg = _time_legacy(queries, data, scale, iterations)
        opt = _time_prepared(queries, data, scale, iterations)
        leg_avg = statistics.mean(leg)
        opt_avg = statistics.mean(opt)
        speedup = ((leg_avg - opt_avg) / leg_avg) * 100.0 if leg_avg else 0.0
        leg_p95 = statistics.quantiles(leg, n=20)[18]
        opt_p95 = statistics.quantiles(opt, n=20)[18]
        print(
            f"{item_count:>6}  {leg_avg:>10.2f}ms  {opt_avg:>12.2f}ms  {speedup:>+7.1f}%"
            f"  {leg_p95:>9.2f}ms  {opt_p95:>7.2f}ms"
        )
    print()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Benchmark OCR locate speed/parity across realistic item counts."
    )
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--tokens", type=int, default=1500,
                   help="Number of OCR tokens in one scan pass (realistic: 500-2000)")
    p.add_argument("--queries", type=int, default=50,
                   help="Max item count to test (also the last row)")
    p.add_argument("--iterations", type=int, default=40,
                   help="Timing iterations per scenario")
    p.add_argument("--scale", type=int, default=1)
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_benchmark(
        seed=args.seed,
        tokens=args.tokens,
        queries_n=args.queries,
        iterations=args.iterations,
        scale=args.scale,
    )