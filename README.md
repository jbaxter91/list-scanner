# List Scanner

A Windows desktop tool that continuously OCRs a user-defined region of the screen and checks a list of text items against what it finds. Each item in the list turns **green** when found, **red** when not found, and the matching text on screen is highlighted with a coloured box overlay in real-time.

---

## How it works

1. You define a **scan area** by dragging a selection box across any part of your screen (including across multiple monitors).
2. You paste a list of items — one per line — into the text box.
3. You press **Start**. Every ~0.75 seconds the tool:
   - Takes a screenshot of the scan area using Windows DWM (overlay is excluded from capture automatically).
   - Converts it to grayscale (red channel only, which gives better contrast for orange/amber/white text on dark backgrounds).
   - Splits it into adaptive tiles and runs Tesseract OCR on each tile in parallel.
   - Searches for every item in the OCR result.
   - Colors each list item green or red and draws highlight boxes over found text on screen.

---

## Technologies Used

| Dependency | Purpose |
|---|---|
| **Tesseract OCR** (system install or bundled in exe) | The OCR engine. Uses LSTM mode (`--oem 1`) and sparse text layout (`--psm 11`) for speed. |
| `pytesseract` | Python wrapper around Tesseract. |
| `Pillow` (PIL) | Image manipulation — grayscale conversion, resize, tile cropping, debug thumbnails. |
| `mss` | Fast screen capture fallback (primary capture uses `ImageGrab` for DWM exclusion). |
| `customtkinter` | Modern-looking dark-mode UI built on top of tkinter. |
| `pynput` | Global keyboard listener (hotkeys) and mouse listener (click/scroll resets). |
| Windows `ctypes` | Low-level Win32 hooks: raw input wheel sink, WinEvent scroll hook, DPI awareness, overlay transparency, DWM capture exclusion (`SetWindowDisplayAffinity`). |
| `concurrent.futures.ThreadPoolExecutor` | Parallel OCR across image tiles. |
| `PyInstaller` (build only) | Packages the app and bundled Tesseract into a single `dist/ListScanner.exe`. |

---

## Requirements

- **Windows 10** or later (Win32 APIs are used extensively).
- **Python 3.10+** (when running from source).
- **Tesseract OCR** — install from [UB-Mannheim builds](https://github.com/UB-Mannheim/tesseract/wiki) to the default location (`C:\Program Files\Tesseract-OCR`). Not needed when using the pre-built exe (Tesseract is bundled).

---

## Running from Source

```bat
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run
python main.py
```

---

## Building the Exe

The build script handles everything — it creates a `.venv`, installs deps, locates your Tesseract install, and produces `dist/ListScanner.exe` with Tesseract bundled inside.

```bat
py build.py
```

Or use the provided batch wrapper:

```bat
build.bat
```

> **Note:** Close `ListScanner.exe` before rebuilding — Windows locks the file while the process is running.

You can override the Tesseract location by setting the environment variable `TESSERACT_DIR` before running the build:

```bat
set TESSERACT_DIR=C:\MyCustomPath\Tesseract-OCR
py build.py
```

---

## Using the App

### 1. Set a Scan Area

Click **Set Scan Area** (or press `F10`) to open a full-screen translucent overlay spanning all monitors:

- **Drag** to select a custom region.
- **Click** (without dragging) to capture the entire monitor you clicked on.
- **Esc** to cancel without changing the current area.

The scan area persists to `list_scanner_config.json` and is restored on next launch.

### 2. Add Items

Paste your list of items into the text box — one item per line. Items can be single words or multi-word phrases.

### 3. Start Scanning

Click **▶ Start** (or press `F12`). While scanning:

- Items turn **green** when found, **red** when not found.
- A coloured box is drawn directly on screen over each matched word/phrase.
- The textbox becomes read-only. Click **⏹ Stop** or press `F12` again to edit the list.

### 4. Click Found Items

Press `F8` to automatically click the center of every currently-found highlight box. Hold Ctrl during click by enabling the **Ctrl+Click** option in settings.

---

## Hotkeys (all rebindable in Settings)

| Default Key | Action |
|---|---|
| `F12` | Start / Stop scanning |
| `F11` | Toggle the on-screen highlight overlay |
| `F10` | Open the scan area selector |
| `F9` | Open / close the Debug window |
| `F8` | Click the center of all found highlight boxes |
| `F7` | Toggle **Additive Mode** |

All hotkeys are global (work even when the app is not focused) and fire on key release.

---

## Additive Mode

Additive mode changes how the scanner decides an item is **found**. It is designed for scenarios where the content on screen scrolls or changes — you scroll through a list and the scanner accumulates evidence across multiple frames rather than judging each frame independently.

**How it works:**

- Toggle it with the **`+`** button in the title bar or press `F7`. The header shows **"additive"** in orange when active.
- On each scan pass, if an item is found it earns a **star** (shown appended to the item text: `Item *`, `Item **`, etc.).
- After **3 consecutive frames** where the item was found it becomes permanently **locked green** for that session — even if the text scrolls off screen. Locked items stay green through subsequent scroll/click resets.
- Items with 0 stars show as red (not yet found), items with 1–2 stars show as **yellow** (building evidence), items with 3+ stars show as **green** (locked).
- Turning additive mode off resets all star counts and restores normal text in the list.

**When to use it:** When you need to scan through a long scrolling list and want each item to "check off" permanently as it appears on screen, rather than items toggling back to red when they scroll away.

---

## Settings (Config Window)

Open with the **⚙** gear button in the title bar.

| Setting | Description |
|---|---|
| **Hotkeys** | Rebind any of the 6 actions. Click a field and press the desired key. |
| **Use Ctrl+Click** | When pressing `F8` to click found items, hold Ctrl during each click (useful for multi-select in some apps). |
| **Always keep window on top** | Keeps the List Scanner window above all other windows. |
| **Max OCR frames** | Maximum number of tiles the image is split into for parallel OCR. Capped at `CPU count × 2` at runtime. Higher values can improve accuracy on large scan areas at the cost of more CPU. Default: `64`. |
| **OCR frame size (px)** | Target tile size in pixels. Smaller values produce more (smaller) tiles. Default: `400`. |
| **OCR frame overlap (px)** | How many pixels tiles overlap at their edges. Prevents words near tile boundaries from being missed. Automatically increased for longer search terms. Default: `30`. |
| **Window opacity** | Transparency of the List Scanner window itself (20–100%). Default: `97`. |
| **Highlight box color** | Color of the rectangles drawn on screen over found text. Click the swatch or `…` to open a color picker. Default: `#da0cda` (magenta). |
| **Highlight box width** | Line thickness (in pixels) of the highlight rectangles. Default: `4`. |

Settings are saved immediately to `list_scanner_config.json` on clicking **Save**.

---

## Config File (`list_scanner_config.json`)

The config file lives next to the script/exe and is created or updated automatically. You can edit it manually while the app is closed.

```jsonc
{
  "scan_area": {          // Last saved scan region (pixels, absolute screen coords)
    "left": 284,
    "top": 255,
    "width": 2133,
    "height": 1205
  },
  "hotkeys": {            // Key names as reported by pynput (e.g. "f12", "a", "space")
    "start_stop":      "f12",
    "toggle_overlay":  "f11",
    "toggle_debug":    "f9",
    "set_area":        "f10",
    "click_found":     "f8",
    "toggle_additive": "f7"
  },
  "ocr_scale": 2.0,       // Upscale factor applied to the screenshot before OCR (higher = more accurate but slower)
  "ctrl_click": true,     // Use Ctrl+Click when clicking found items
  "always_on_top": true,  // Keep List Scanner above all other windows
  "ocr_tile_max": 64,     // Max parallel OCR tiles (capped at CPU count × 2 at runtime)
  "ocr_tile_target_px": 400, // Target tile size in pixels
  "ocr_tile_overlap_px": 30, // Tile edge overlap in pixels
  "opacity": 97,          // Window opacity percentage (20–100)
  "box_color": "#da0cda", // Hex color for highlight boxes drawn on screen
  "box_width": 4          // Line width (px) of highlight boxes
}
```

---

## Debug Window

Open with `F9` or via the **Debug Window** button in Settings.

- **OCR Scale slider** — Adjust the upscale factor live without restarting a scan.
- **Show last screenshot** — Displays a thumbnail of the most recent captured frame.
- **Show OCR frames** — Overlays orange rectangles on the thumbnail showing how the image was tiled.
- **Scan Log tab** — Per-scan results: which items were found/missed, vote history, timing.
- **Debug Log tab** — Timestamped events: scan start/stop, vote resets, hotkey toggles, errors.

---

## Project Structure

```
main.py                    # UI, scan loop, overlays, hotkeys, config
ocr_engine.py              # Pure OCR logic: tiling, parallel OCR, text search
list_scanner_config.json   # Persisted user settings
requirements.txt           # Python dependencies
build.py                   # PyInstaller build script
build.bat                  # Convenience wrapper for build.py
ListScanner.spec           # PyInstaller spec (generated by build.py)
```
