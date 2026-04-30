"""Simulate one pass of the scan loop to expose any errors."""
import ctypes
import traceback

# Same DPI awareness as main.py
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from PIL import Image, ImageGrab
import pytesseract
import os, sys

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Use primary monitor top-left corner as scan area
area = {"left": 0, "top": 0, "width": 400, "height": 300}
scale = 0.9

try:
    bbox = (area["left"], area["top"], area["left"] + area["width"], area["top"] + area["height"])
    print(f"Grabbing bbox={bbox}")
    orig_img = ImageGrab.grab(bbox=bbox, all_screens=True)
    print(f"Grabbed: {orig_img.size} {orig_img.mode}")

    gray = orig_img.convert("L")
    w, h = gray.size
    new_w, new_h = int(w * scale), int(h * scale)
    print(f"Resizing {w}x{h} -> {new_w}x{new_h}")
    img = gray.resize((new_w, new_h), Image.BILINEAR)
    print(f"Resized ok")

    ocr = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config="--oem 1 --psm 11")
    print(f"OCR ok, {len(ocr['text'])} words")

    print("ALL GOOD")
except Exception:
    print("EXCEPTION:")
    traceback.print_exc()
