"""
Real-image OCR benchmark using TestIMG.png.

Replicates the exact scan-loop pipeline from main.py:
  1. Load image (simulates ImageGrab)
  2. Convert to grayscale
  3. Resize by ocr_scale
  4. pytesseract.image_to_data with --oem 1 --psm 11 lang=eng
  5. _prepare_ocr_index
  6. _locate_prepared per keyword

Reports per-phase timing (ms) and accuracy (found/not found + bounding boxes)
across multiple iterations to give stable averages.
"""
from __future__ import annotations

import argparse
import statistics
import time
from collections import deque
from pathlib import Path

from PIL import Image
import pytesseract

import main

# ---------------------------------------------------------------------------
# Tesseract path (mirrors main.py detection logic)
# ---------------------------------------------------------------------------
import os, sys

if getattr(sys, "frozen", False):
    _bundled = os.path.join(sys._MEIPASS, "tesseract", "tesseract.exe")
    pytesseract.pytesseract.tesseract_cmd = _bundled
    os.environ["TESSDATA_PREFIX"] = os.path.join(sys._MEIPASS, "tesseract", "tessdata")
elif sys.platform == "win32":
    for _p in [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR", "tesseract.exe"),
    ]:
        if os.path.exists(_p):
            pytesseract.pytesseract.tesseract_cmd = _p
            break

# ---------------------------------------------------------------------------
# Keywords and image
# ---------------------------------------------------------------------------
KEYWORDS = [".exe", ".jar", "luffy", "claude", "poop"]
IMAGE_PATH = Path(__file__).parent / "TestIMG.png"


def _is_all_numeric(keywords: list[str]) -> bool:
    return all(
        bool(k.strip()) and "".join(c for c in k if not c.isspace()).isdigit()
        for k in keywords
    )


def _preprocess_all(keywords: list[str]) -> list[dict]:
    return [main._preprocess_search_term(k) for k in keywords]


def run_one_pass(img_orig: Image.Image, scale: float, ocr_config: str) -> dict:
    """
    Run a single complete scan pass — identical to the scan loop in main.py.
    Returns a dict with timing (ms) for each phase and OCR result dict.
    """
    t_pre_start = time.perf_counter()
    gray = img_orig.convert("L")
    w, h = gray.size
    img = gray.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
    pre_ms = (time.perf_counter() - t_pre_start) * 1000

    t_ocr_start = time.perf_counter()
    ocr = pytesseract.image_to_data(
        img,
        output_type=pytesseract.Output.DICT,
        lang="eng",
        config=ocr_config,
    )
    ocr_ms = (time.perf_counter() - t_ocr_start) * 1000

    t_idx_start = time.perf_counter()
    prepared = main._prepare_ocr_index(ocr, scale)
    idx_ms = (time.perf_counter() - t_idx_start) * 1000

    return {
        "pre_ms": pre_ms,
        "ocr_ms": ocr_ms,
        "idx_ms": idx_ms,
        "prepared": prepared,
        "token_count": sum(1 for t in ocr["text"] if t.strip()),
    }


_OCR_CONFIG = "--oem 1 --psm 11"


def run_benchmark(scale: float, iterations: int):
    if not IMAGE_PATH.exists():
        print(f"ERROR: {IMAGE_PATH} not found. Place TestIMG.png in the project root.")
        return

    digits_only = _is_all_numeric(KEYWORDS)
    suffix = " -c tessedit_char_whitelist=0123456789" if digits_only else ""
    ocr_config = _OCR_CONFIG + suffix
    preps = _preprocess_all(KEYWORDS)

    print("=" * 72)
    print(f"Real-image OCR benchmark  —  {IMAGE_PATH.name}")
    print(f"scale={scale}x  lang=eng  {'digits-only mode' if digits_only else 'general mode'}")
    print(f"keywords={KEYWORDS}")
    print(f"iterations={iterations}")
    print("=" * 72)

    # ── Load image once (simulates ImageGrab having already fired) ──────────
    img_orig = Image.open(IMAGE_PATH)
    print(f"Image size: {img_orig.width}x{img_orig.height}  mode={img_orig.mode}")
    print()

    def _run_passes(label: str, use_frame_cache: bool) -> dict:
        """Run iterations of the full pipeline and return timing/accuracy results."""
        # Warm-up (discarded)
        run_one_pass(img_orig, scale, ocr_config)

        pre_times:   list[float] = []
        ocr_times:   list[float] = []
        total_times: list[float] = []
        token_counts: list[int] = []
        cache_hits = 0

        WINDOW = 4
        votes = [deque(maxlen=WINDOW) for _ in KEYWORDS]
        last_hits: list[list[tuple]] = [[] for _ in KEYWORDS]

        _last_hash: int | None = None
        _cached_prep: dict | None = None
        _cached_token_count: int = 0

        for _ in range(iterations):
            t_total = time.perf_counter()

            # Phase 1: preprocess (always runs — same as scan loop)
            t_pre = time.perf_counter()
            gray = img_orig.convert("L")
            w, h = gray.size
            img_resized = gray.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
            pre_ms = (time.perf_counter() - t_pre) * 1000

            if use_frame_cache:
                img_hash = hash(img_resized.tobytes())
                if _last_hash == img_hash and _cached_prep is not None:
                    # Cache hit — skip OCR entirely
                    prepared = _cached_prep
                    actual_ocr_ms = 0.0
                    token_count = _cached_token_count
                    cache_hits += 1
                else:
                    # Cache miss — full OCR
                    t_ocr = time.perf_counter()
                    ocr = pytesseract.image_to_data(
                        img_resized,
                        output_type=pytesseract.Output.DICT,
                        lang="eng",
                        config=ocr_config,
                    )
                    actual_ocr_ms = (time.perf_counter() - t_ocr) * 1000
                    prepared = main._prepare_ocr_index(ocr, scale)
                    token_count = len([t for t in ocr["text"] if t.strip()])
                    _last_hash = img_hash
                    _cached_prep = prepared
                    _cached_token_count = token_count
            else:
                result = run_one_pass(img_orig, scale, ocr_config)
                prepared = result["prepared"]
                actual_ocr_ms = result["ocr_ms"]
                token_count = result["token_count"]

            for ki, (kw, prep) in enumerate(zip(KEYWORDS, preps)):
                hits = main._locate_prepared(kw, prepared, prep)
                votes[ki].append(1 if hits else 0)
                if hits:
                    last_hits[ki] = hits

            elapsed_ms = (time.perf_counter() - t_total) * 1000
            pre_times.append(pre_ms)
            ocr_times.append(actual_ocr_ms)
            total_times.append(elapsed_ms)
            token_counts.append(token_count)

        return {
            "label": label,
            "pre_times": pre_times,
            "ocr_times": ocr_times,
            "total_times": total_times,
            "token_counts": token_counts,
            "votes": votes,
            "last_hits": last_hits,
            "cache_hits": cache_hits,
            "prepared": _cached_prep or prepared,
        }

    def _fmt(times: list[float]) -> str:
        avg = statistics.mean(times)
        p95 = statistics.quantiles(times, n=20)[18]
        mn  = min(times)
        return f"avg={avg:7.1f}ms  min={mn:6.1f}ms  p95={p95:7.1f}ms"

    def _print_result(r: dict):
        label = r["label"]
        print(f"── {label} ──")
        print(f"  preprocess  : {_fmt(r['pre_times'])}")
        print(f"  OCR call    : {_fmt(r['ocr_times'])}  (cache_hits={r['cache_hits']}/{iterations})")
        print(f"  TOTAL/pass  : {_fmt(r['total_times'])}")
        print(f"  avg tokens  : {statistics.mean(r['token_counts']):.0f}")
        print()
        print(f"  Accuracy ({label}):")
        print(f"    {'keyword':<18}  {'status':<10}  {'votes':>6}  bounding boxes")
        print(f"    {'-'*18}  {'-'*10}  {'-'*6}  {'-'*38}")
        for ki, kw in enumerate(KEYWORDS):
            n = len(r["votes"][ki])
            vsum = sum(r["votes"][ki])
            status = "FOUND" if vsum > n / 2 else "NOT FOUND"
            boxes = r["last_hits"][ki] if status == "FOUND" else []
            box_str = ", ".join(str(b) for b in boxes[:2])
            if len(boxes) > 2:
                box_str += f"  (+{len(boxes)-2} more)"
            print(f"    {kw:<18}  {status:<10}  {vsum:>2}/{n:<3}  {box_str}")
        print()
        norm_words = r["prepared"]["norm_words"]
        valid = r["prepared"]["valid"]
        ocr_words = [norm_words[i] for i in valid if norm_words[i]]
        print(f"  OCR words (conf≥30): {len(ocr_words)} total — first 48 shown:")
        chunk = [f"'{w}'" for w in ocr_words[:48]]
        for row in range(0, len(chunk), 8):
            print("    " + "  ".join(chunk[row:row+8]))
        print()

    baseline = _run_passes("baseline (OCR every pass)", use_frame_cache=False)
    cached   = _run_passes("frame-hash cache (static screen)", use_frame_cache=True)

    _print_result(baseline)
    _print_result(cached)

    # ── Summary ──────────────────────────────────────────────────────
    base_ocr = statistics.mean(baseline["ocr_times"])
    cache_ocr = statistics.mean(cached["ocr_times"])
    base_total = statistics.mean(baseline["total_times"])
    cache_total = statistics.mean(cached["total_times"])
    print("=" * 72)
    print("Frame-hash cache speedup (static screen, worst case = same frame every pass):")
    print(f"  baseline OCR avg            : {base_ocr:7.1f}ms")
    print(f"  cached OCR avg              : {cache_ocr:7.1f}ms  ({(iterations - 1)}/{iterations} passes skipped)")
    print(f"  baseline total avg/pass     : {base_total:7.1f}ms")
    print(f"  cached total avg/pass       : {cache_total:7.1f}ms")
    saved_ms = base_total - cache_total
    pct = (saved_ms / base_total) * 100 if base_total else 0
    print(f"  saved per pass (steady state): {saved_ms:6.1f}ms  ({pct:.1f}% faster)")
    print("=" * 72)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Real-image OCR benchmark using TestIMG.png")
    p.add_argument("--scale", type=float, default=0.9,
                   help="OCR scale factor (default matches app default: 0.9)")
    p.add_argument("--iterations", type=int, default=10,
                   help="Number of timed passes (default: 10)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_benchmark(scale=args.scale, iterations=args.iterations)
