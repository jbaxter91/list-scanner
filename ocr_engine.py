from __future__ import annotations

from dataclasses import dataclass
import math
import re as _re
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image
import pytesseract


@dataclass
class OcrPassResult:
    prepared_ocr: dict
    words: list[str]
    ocr_ms: float
    tile_desc: str
    tiles: list[dict]


class OcrEngine:
    """Pure OCR orchestration: tiling, parallel OCR, and index preparation."""

    def run_pass(
        self,
        gray: Image.Image,
        scale: float,
        target_px: int,
        min_tile_px: int,
        overlap_x_px: int,
        overlap_y_px: int,
        max_tiles: int,
    ) -> OcrPassResult:
        ocr_start = time.perf_counter()

        # --oem 1: LSTM engine only (fastest accurate mode)
        # --psm 11: sparse text - finds words anywhere on the page
        ocr_config = "--oem 1 --psm 11"

        w, h = gray.size
        cols, rows, tiles = self.build_adaptive_tiles(
            width=w,
            height=h,
            target_px=target_px,
            min_tile_px=min_tile_px,
            overlap_x_px=overlap_x_px,
            overlap_y_px=overlap_y_px,
            max_tiles=max_tiles,
        )
        tile_desc = f"{cols}x{rows} ({len(tiles)} tile{'s' if len(tiles) != 1 else ''})"

        if len(tiles) == 1:
            img = gray.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
            ocr = pytesseract.image_to_data(
                img,
                output_type=pytesseract.Output.DICT,
                lang="eng",
                config=ocr_config,
            )
        else:
            jobs: list[tuple[dict, Image.Image]] = []
            for tile in tiles:
                left = tile["left"]
                top = tile["top"]
                right = left + tile["width"]
                bottom = top + tile["height"]
                jobs.append((tile, gray.crop((left, top, right, bottom))))

            worker_count = min(len(jobs), max_tiles)
            with ThreadPoolExecutor(max_workers=worker_count) as ex:
                parts = list(
                    ex.map(
                        lambda job: self.ocr_tile(job, scale, ocr_config),
                        jobs,
                    )
                )
            ocr = empty_ocr_result()
            for part in parts:
                for k in ("text", "conf", "left", "top", "width", "height"):
                    ocr[k].extend(part[k])

        ocr_ms = (time.perf_counter() - ocr_start) * 1000
        prepared_ocr = prepare_ocr_index(ocr, scale)
        words = [
            text.strip()
            for text, conf in zip(ocr.get("text", []), ocr.get("conf", []))
            if text and text.strip() and str(conf).strip() != "-1"
        ]

        return OcrPassResult(
            prepared_ocr=prepared_ocr,
            words=words,
            ocr_ms=ocr_ms,
            tile_desc=tile_desc,
            tiles=tiles,
        )

    @staticmethod
    def build_adaptive_tiles(
        width: int,
        height: int,
        target_px: int,
        min_tile_px: int,
        overlap_x_px: int,
        overlap_y_px: int,
        max_tiles: int,
    ) -> tuple[int, int, list[dict]]:
        step_x = max(1, target_px - overlap_x_px)
        step_y = max(1, target_px - overlap_y_px)
        cols = max(1, math.ceil(max(1, width - overlap_x_px) / step_x))
        rows = max(1, math.ceil(max(1, height - overlap_y_px) / step_y))

        while cols > 1 and (width / cols) < min_tile_px:
            cols -= 1
        while rows > 1 and (height / rows) < min_tile_px:
            rows -= 1

        while rows * cols > max_tiles:
            if cols >= rows and cols > 1:
                cols -= 1
            elif rows > 1:
                rows -= 1
            else:
                break

        base_w = (width + cols - 1) // cols
        base_h = (height + rows - 1) // rows
        overlap_x = max(0, overlap_x_px)
        overlap_y = max(0, overlap_y_px)

        tiles: list[dict] = []
        for r in range(rows):
            for c in range(cols):
                x0 = c * base_w
                y0 = r * base_h
                x1 = min(width, (c + 1) * base_w)
                y1 = min(height, (r + 1) * base_h)

                tx0 = max(0, x0 - overlap_x)
                ty0 = max(0, y0 - overlap_y)
                tx1 = min(width, x1 + overlap_x)
                ty1 = min(height, y1 + overlap_y)

                tiles.append(
                    {
                        "left": tx0,
                        "top": ty0,
                        "width": tx1 - tx0,
                        "height": ty1 - ty0,
                    }
                )

        return cols, rows, tiles

    @staticmethod
    def ocr_tile(job: tuple[dict, Image.Image], scale: float, ocr_config: str) -> dict:
        tile, crop = job
        tw, th = crop.size
        resized = crop.resize((int(tw * scale), int(th * scale)), Image.BILINEAR)
        ocr = pytesseract.image_to_data(
            resized,
            output_type=pytesseract.Output.DICT,
            lang="eng",
            config=ocr_config,
        )

        offset_x = int(tile["left"] * scale)
        offset_y = int(tile["top"] * scale)
        merged = empty_ocr_result()
        n = len(ocr.get("text", []))
        for i in range(n):
            merged["text"].append(ocr["text"][i])
            merged["conf"].append(ocr["conf"][i])
            merged["left"].append(int(ocr["left"][i]) + offset_x)
            merged["top"].append(int(ocr["top"][i]) + offset_y)
            merged["width"].append(int(ocr["width"][i]))
            merged["height"].append(int(ocr["height"][i]))
        return merged


def empty_ocr_result() -> dict:
    return {
        "text": [],
        "conf": [],
        "left": [],
        "top": [],
        "width": [],
        "height": [],
    }


def prepare_ocr_index(data: dict, scale: float = 1) -> dict:
    """Precompute normalized OCR tokens and scaled geometry once per scan pass."""
    words = data["text"]
    confs = data["conf"]
    left = data["left"]
    top = data["top"]
    width = data["width"]
    height = data["height"]

    norm_words = [norm(w) if w.strip() else "" for w in words]
    valid = [
        i for i, (norm_word, conf) in enumerate(zip(norm_words, confs))
        if norm_word and safe_int(conf) >= 30
    ]

    scaled_left = [int(x / scale) for x in left]
    scaled_top = [int(y / scale) for y in top]
    scaled_width = [int(w / scale) for w in width]
    scaled_height = [int(h / scale) for h in height]
    scaled_right = [int((x + w) / scale) for x, w in zip(left, width)]
    scaled_bottom = [int((y + h) / scale) for y, h in zip(top, height)]

    return {
        "valid": valid,
        "norm_words": norm_words,
        "scaled_left": scaled_left,
        "scaled_top": scaled_top,
        "scaled_width": scaled_width,
        "scaled_height": scaled_height,
        "scaled_right": scaled_right,
        "scaled_bottom": scaled_bottom,
    }


def scan_tokens_prepared(q_words: list[str], prepared: dict) -> list[tuple]:
    valid = prepared["valid"]
    norm_words = prepared["norm_words"]
    scaled_left = prepared["scaled_left"]
    scaled_top = prepared["scaled_top"]
    scaled_width = prepared["scaled_width"]
    scaled_height = prepared["scaled_height"]
    scaled_right = prepared["scaled_right"]
    scaled_bottom = prepared["scaled_bottom"]

    m = len(q_words)
    hits = []
    if m == 1:
        q = q_words[0]
        for idx in valid:
            if q in norm_words[idx]:
                hits.append((
                    scaled_left[idx],
                    scaled_top[idx],
                    scaled_width[idx],
                    scaled_height[idx],
                ))
    else:
        for k in range(len(valid) - m + 1):
            ids = [valid[k + j] for j in range(m)]
            chunk = [norm_words[i] for i in ids]
            if all(qw in c for qw, c in zip(q_words, chunk)):
                x0 = min(scaled_left[i] for i in ids)
                y0 = min(scaled_top[i] for i in ids)
                x1 = max(scaled_right[i] for i in ids)
                y1 = max(scaled_bottom[i] for i in ids)
                hits.append((x0, y0, x1 - x0, y1 - y0))
    return hits


def preprocess_search_term(search: str) -> dict:
    query = [norm(w) for w in search.split() if w.strip()]
    sep_query = [norm(w) for w in _re.split(r'[.\-_]+', search) if w.strip()]
    return {
        "query": query,
        "sep_query": sep_query,
    }


def locate_prepared(search: str, prepared: dict, search_prep: dict | None = None) -> list[tuple]:
    prep = search_prep if search_prep is not None else preprocess_search_term(search)
    query = prep.get("query", [])
    if not query:
        return []

    sep_query = prep.get("sep_query", [])

    results = scan_tokens_prepared(query, prepared)
    if not results and sep_query != query:
        results = scan_tokens_prepared(sep_query, prepared)
    return results


def locate(search: str, data: dict, scale: float = 1) -> list[tuple]:
    prepared = prepare_ocr_index(data, scale)
    return locate_prepared(search, prepared)


def norm(s: str) -> str:
    return s.lower().strip().strip('.,;:!?"\'-')


def safe_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
