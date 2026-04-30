"""
List Scanner build script.

Run with:
    py build.py

Creates/uses .venv, installs dependencies, locates Tesseract, and produces:
    dist/ListScanner.exe
"""

import os
import subprocess
import sys


ROOT = os.path.dirname(os.path.abspath(__file__))

VENV_DIR = os.path.join(ROOT, ".venv")
PYTHON = os.path.join(VENV_DIR, "Scripts", "python.exe")
PIP = os.path.join(VENV_DIR, "Scripts", "pip.exe")
PYINSTALLER = os.path.join(VENV_DIR, "Scripts", "pyinstaller.exe")

EXE_NAME = "ListScanner.exe"
DIST_EXE = os.path.join(ROOT, "dist", EXE_NAME)

TESS_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR",
    r"C:\Program Files (x86)\Tesseract-OCR",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR"),
]

SEP = "=" * 50


def pause_if_interactive():
    if sys.stdin.isatty():
        input("\nPress Enter to exit...")


def fail(message, exit_code=1):
    print(f"\nERROR: {message}")
    pause_if_interactive()
    sys.exit(exit_code)


def run(cmd, **kwargs):
    print(f"  > {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        fail(f"command failed with exit code {result.returncode}", result.returncode)


def running_listscanner_pids():
    """Return PIDs for running ListScanner.exe processes, if PowerShell is available."""
    ps_command = (
        "Get-Process -Name ListScanner -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Path -eq '{DIST_EXE}' }} | "
        "ForEach-Object { $_.Id }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_command],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []

    if result.returncode != 0:
        return []

    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def ensure_dist_exe_replaceable():
    if not os.path.exists(DIST_EXE):
        return

    temp_exe = f"{DIST_EXE}.buildcheck.{os.getpid()}"
    try:
        os.replace(DIST_EXE, temp_exe)
        os.replace(temp_exe, DIST_EXE)
    except PermissionError:
        pids = running_listscanner_pids()
        pid_text = f"\nRunning PID(s): {', '.join(pids)}" if pids else ""
        fail(
            f"{EXE_NAME} is currently locked, so Windows will not let PyInstaller "
            f"replace it.\nClose the app first, then run the build again.{pid_text}"
        )
    finally:
        if os.path.exists(temp_exe) and not os.path.exists(DIST_EXE):
            os.replace(temp_exe, DIST_EXE)


def locate_tesseract():
    tess_dir = os.environ.get("TESSERACT_DIR", "").strip()
    if tess_dir and os.path.isfile(os.path.join(tess_dir, "tesseract.exe")):
        return tess_dir

    for candidate in TESS_CANDIDATES:
        if os.path.isfile(os.path.join(candidate, "tesseract.exe")):
            return candidate

    return ""


def main():
    print(SEP)
    print("  List Scanner - Build Script")
    print(SEP)

    os.chdir(ROOT)

    print("\n[1/4] Checking virtual environment...")
    if not os.path.isfile(PYTHON):
        print("  Creating virtual environment...")
        run([sys.executable, "-m", "venv", ".venv"])
    else:
        print("  Virtual environment already exists, skipping creation.")

    print("\n[2/4] Installing Python dependencies...")
    run([PIP, "install", "-q", "-r", "requirements.txt"])
    run([PIP, "install", "-q", "pyinstaller"])

    print("\n[3/4] Locating Tesseract OCR...")
    tess_dir = locate_tesseract()
    if not tess_dir:
        fail(
            "Tesseract OCR was not found on this machine.\n"
            "The build machine must have Tesseract installed so it can be bundled.\n"
            "Download: https://github.com/UB-Mannheim/tesseract/wiki\n"
            r"Default install path: C:\Program Files\Tesseract-OCR"
        )

    print(f"  Found Tesseract at: {tess_dir}")
    os.environ["TESSERACT_DIR"] = tess_dir

    print("\n[4/4] Building executable (this may take a minute)...")
    ensure_dist_exe_replaceable()

    run([PYINSTALLER, "--clean", "ListScanner.spec"])

    print()
    if os.path.isfile(DIST_EXE):
        print(SEP)
        print("  BUILD SUCCESSFUL")
        print(r"  Executable: dist\ListScanner.exe")
        print("  Tesseract is bundled - users need nothing extra.")
        print(SEP)
    else:
        print(SEP)
        print("  BUILD FAILED - check output above")
        print(SEP)

    pause_if_interactive()


if __name__ == "__main__":
    main()
