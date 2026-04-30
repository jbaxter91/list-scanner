from __future__ import annotations

"""
Benchmark tiled parallel OCR on TestIMG.png.

Compares full-image OCR against tiled OCR with overlap using threads:
- 2 tiles (1x2)
- 4 tiles (2x2)
- 8 tiles (2x4)

Each mode uses the same preprocessing / locate helpers from main.py to keep
matching behavior consistent.
"""

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image
import pytesseract

import main

# ---------------------------------------------------------------------------
# Tesseract path (mirrors main.py / benchmark_realimage.py logic)
# ---------------------------------------------------------------------------
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

KEYWORDS = [".exe", ".jar", "luffy", "claude", "poop"]
IMAGE_PATH = Path(__file__).parent / "TestIMG.png"
_OCR_CONFIG = "--oem 1 --psm 11"


def _is_all_numeric(keywords: list[str]) -> bool:
    return all(
        bool(k.strip()) and "".join(c for c in k if not c.isspace()).isdigit()
        for k in keywords
    )


def _preprocess_all(keywords: list[str]) -> list[dict]:
    return [main._preprocess_search_term(k) for k in keywords]


def _grid_for_tile_count(tile_count: int) -> tuple[int, int]:
    if tile_count == 2:
        return (1, 2)
    if tile_count == 4:
        return (2, 2)
    if tile_count == 8:
        return (2, 4)
    if tile_count == 16:
        return (4, 4)
    raise ValueError(f"Unsupported tile_count={tile_count}; expected one of: 2, 4, 8, 16")


def _build_tiles(width: int, height: int, tile_count: int, overlap: int) -> list[dict]:
    rows, cols = _grid_for_tile_count(tile_count)
    base_w = (width + cols - 1) // cols
    base_h = (height + rows - 1) // rows

    tiles: list[dict] = []
    for r in range(rows):
        for c in range(cols):
            x0 = c * base_w
            y0 = r * base_h
            x1 = min(width, (c + 1) * base_w)
            y1 = min(height, (r + 1) * base_h)

            tx0 = max(0, x0 - overlap)
            ty0 = max(0, y0 - overlap)
            tx1 = min(width, x1 + overlap)
            ty1 = min(height, y1 + overlap)

            tiles.append(
                {
                    "row": r,
                    "col": c,
                    "left": tx0,
                    "top": ty0,
                    "width": tx1 - tx0,
                    "height": ty1 - ty0,
                }
            )
    return tiles


def _empty_ocr_dict() -> dict:
    return {
        "text": [],
        "conf": [],
        "left": [],
        "top": [],
        "width": [],
        "height": [],
    }


def _run_ocr_on_image(img: Image.Image, scale: float, ocr_config: str) -> tuple[dict, int, float]:
    w, h = img.size
    resized = img.resize((int(w * scale), int(h * scale)), Image.BILINEAR)

    t0 = time.perf_counter()
    ocr = pytesseract.image_to_data(
        resized,
        output_type=pytesseract.Output.DICT,
        lang="eng",
        config=ocr_config,
    )
    ocr_ms = (time.perf_counter() - t0) * 1000.0
    token_count = sum(1 for t in ocr.get("text", []) if str(t).strip())
    return ocr, token_count, ocr_ms


def _run_tile_worker(gray: Image.Image, tile: dict, scale: float, ocr_config: str) -> dict:
    left = tile["left"]
    top = tile["top"]
    right = left + tile["width"]
    bottom = top + tile["height"]

    crop = gray.crop((left, top, right, bottom))
    ocr, token_count, ocr_ms = _run_ocr_on_image(crop, scale, ocr_config)

    offset_x_scaled = int(left * scale)
    offset_y_scaled = int(top * scale)

    merged = _empty_ocr_dict()
    n = len(ocr.get("text", []))
    for i in range(n):
        merged["text"].append(ocr["text"][i])
        merged["conf"].append(ocr["conf"][i])
        merged["left"].append(int(ocr["left"][i]) + offset_x_scaled)
        merged["top"].append(int(ocr["top"][i]) + offset_y_scaled)
        merged["width"].append(int(ocr["width"][i]))
        merged["height"].append(int(ocr["height"][i]))

    return {
        "ocr": merged,
        "token_count": token_count,
        "ocr_ms": ocr_ms,
    }


def _merge_ocr_dicts(parts: list[dict]) -> dict:
    merged = _empty_ocr_dict()
    for part in parts:
        ocr = part["ocr"]
        merged["text"].extend(ocr["text"])
        merged["conf"].extend(ocr["conf"])
        merged["left"].extend(ocr["left"])
        merged["top"].extend(ocr["top"])
        merged["width"].extend(ocr["width"])
        merged["height"].extend(ocr["height"])
    return merged


def _evaluate_keywords(prepared: dict, preps: list[dict]) -> dict:
    statuses: dict[str, bool] = {}
    box_counts: dict[str, int] = {}
    for kw, prep in zip(KEYWORDS, preps):
        hits = main._locate_prepared(kw, prepared, prep)
        statuses[kw] = bool(hits)
        box_counts[kw] = len(hits)
    return {"statuses": statuses, "box_counts": box_counts}


def run_full_pass(gray: Image.Image, scale: float, ocr_config: str, preps: list[dict]) -> dict:
    t0 = time.perf_counter()
    ocr, token_count, ocr_ms = _run_ocr_on_image(gray, scale, ocr_config)
    prepared = main._prepare_ocr_index(ocr, scale)
    eval_result = _evaluate_keywords(prepared, preps)
    total_ms = (time.perf_counter() - t0) * 1000.0

    return {
        "mode": "full",
        "total_ms": total_ms,
        "ocr_wall_ms": ocr_ms,
        "ocr_sum_ms": ocr_ms,
        "token_count": token_count,
        "statuses": eval_result["statuses"],
        "box_counts": eval_result["box_counts"],
    }


def run_tiled_parallel_pass(
    gray: Image.Image,
    scale: float,
    ocr_config: str,
    preps: list[dict],
    tile_count: int,
    overlap: int,
) -> dict:
    t0 = time.perf_counter()
    tiles = _build_tiles(gray.width, gray.height, tile_count, overlap)

    # OCR runs in parallel threads; pytesseract invokes external tesseract processes.
    with ThreadPoolExecutor(max_workers=tile_count) as ex:
        parts = list(ex.map(lambda t: _run_tile_worker(gray, t, scale, ocr_config), tiles))

    merged_ocr = _merge_ocr_dicts(parts)
    prepared = main._prepare_ocr_index(merged_ocr, scale)
    eval_result = _evaluate_keywords(prepared, preps)

    total_ms = (time.perf_counter() - t0) * 1000.0
    ocr_sum_ms = sum(p["ocr_ms"] for p in parts)
    ocr_wall_ms = max((p["ocr_ms"] for p in parts), default=0.0)
    token_count = sum(p["token_count"] for p in parts)

    return {
        "mode": f"tiles_{tile_count}",
        "total_ms": total_ms,
        "ocr_wall_ms": ocr_wall_ms,
        "ocr_sum_ms": ocr_sum_ms,
        "token_count": token_count,
        "statuses": eval_result["statuses"],
        "box_counts": eval_result["box_counts"],
    }


def _fmt_stats(values: list[float]) -> str:
    avg = statistics.mean(values)
    mn = min(values)
    if len(values) >= 3:
        p95 = statistics.quantiles(values, n=20)[18]
    else:
        p95 = max(values)
    return f"avg={avg:7.1f}ms  min={mn:6.1f}ms  p95={p95:7.1f}ms"


def run_benchmark(scale: float, iterations: int, overlap: int):
    if not IMAGE_PATH.exists():
        print(f"ERROR: {IMAGE_PATH} not found.")
        return

    digits_only = _is_all_numeric(KEYWORDS)
    suffix = " -c tessedit_char_whitelist=0123456789" if digits_only else ""
    ocr_config = _OCR_CONFIG + suffix
    preps = _preprocess_all(KEYWORDS)

    print("=" * 84)
    print(f"Tiled parallel OCR benchmark  —  {IMAGE_PATH.name}")
    print(f"scale={scale}x  overlap={overlap}px  iterations={iterations}  lang=eng")
    print(f"keywords={KEYWORDS}")
    print("modes: full, tiles_2 (1x2), tiles_4 (2x2), tiles_8 (2x4), tiles_16 (4x4)")
    print("=" * 84)

    img_orig = Image.open(IMAGE_PATH)
    gray = img_orig.convert("L")
    print(f"Image size: {gray.width}x{gray.height}")
    print()

    mode_order = ["full", "tiles_2", "tiles_4", "tiles_8", "tiles_16"]
    history: dict[str, list[dict]] = {m: [] for m in mode_order}

    # Warm-up per mode so first-run startup doesn't dominate.
    run_full_pass(gray, scale, ocr_config, preps)
    run_tiled_parallel_pass(gray, scale, ocr_config, preps, tile_count=2, overlap=overlap)
    run_tiled_parallel_pass(gray, scale, ocr_config, preps, tile_count=4, overlap=overlap)
    run_tiled_parallel_pass(gray, scale, ocr_config, preps, tile_count=8, overlap=overlap)
    run_tiled_parallel_pass(gray, scale, ocr_config, preps, tile_count=16, overlap=overlap)

    for _ in range(iterations):
        history["full"].append(run_full_pass(gray, scale, ocr_config, preps))
        history["tiles_2"].append(run_tiled_parallel_pass(gray, scale, ocr_config, preps, tile_count=2, overlap=overlap))
        history["tiles_4"].append(run_tiled_parallel_pass(gray, scale, ocr_config, preps, tile_count=4, overlap=overlap))
        history["tiles_8"].append(run_tiled_parallel_pass(gray, scale, ocr_config, preps, tile_count=8, overlap=overlap))
        history["tiles_16"].append(run_tiled_parallel_pass(gray, scale, ocr_config, preps, tile_count=16, overlap=overlap))

    print("Timing summary:")
    for mode in mode_order:
        total_vals = [r["total_ms"] for r in history[mode]]
        wall_vals = [r["ocr_wall_ms"] for r in history[mode]]
        sum_vals = [r["ocr_sum_ms"] for r in history[mode]]
        tokens = [r["token_count"] for r in history[mode]]
        print(f"- {mode:<8} total     {_fmt_stats(total_vals)}")
        print(f"           ocr_wall  {_fmt_stats(wall_vals)}")
        print(f"           ocr_sum   {_fmt_stats(sum_vals)}")
        print(f"           avg tokens: {statistics.mean(tokens):.0f}")

    full_avg = statistics.mean(r["total_ms"] for r in history["full"])
    print("\nRelative speed vs full (lower total_ms is better):")
    for mode in ["tiles_2", "tiles_4", "tiles_8", "tiles_16"]:
        avg_total = statistics.mean(r["total_ms"] for r in history[mode])
        delta_ms = full_avg - avg_total
        pct = (delta_ms / full_avg) * 100 if full_avg else 0.0
        print(f"- {mode:<8} avg_total={avg_total:7.1f}ms  change={delta_ms:+7.1f}ms ({pct:+.1f}%)")

    print("\nKeyword parity vs full (FOUND status):")
    for kw in KEYWORDS:
        base_votes = sum(1 for r in history["full"] if r["statuses"][kw])
        line = f"- {kw:<12} full={base_votes}/{iterations}"
        for mode in ["tiles_2", "tiles_4", "tiles_8", "tiles_16"]:
            votes = sum(1 for r in history[mode] if r["statuses"][kw])
            line += f"  {mode}={votes}/{iterations}"
        print(line)

    print("\nLast-pass box counts by mode (shows duplicate-hit tendency with overlap):")
    for mode in mode_order:
        last = history[mode][-1]
        counts = ", ".join(f"{kw}:{last['box_counts'][kw]}" for kw in KEYWORDS)
        print(f"- {mode:<8} {counts}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Benchmark tiled parallel OCR on TestIMG.png")
    p.add_argument("--scale", type=float, default=0.9)
    p.add_argument("--iterations", type=int, default=6)
    p.add_argument("--overlap", type=int, default=30)
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_benchmark(scale=args.scale, iterations=args.iterations, overlap=args.overlap)
