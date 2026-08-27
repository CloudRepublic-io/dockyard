#!/usr/bin/env python3
"""
Diagnoses why Dockyard (or the compose-push agent) isn't finding compose
files it should be finding. Run this INSIDE the same container/environment
where the scan is failing - it uses the exact same file-discovery logic, so
whatever it reports is exactly what Dockyard itself sees.

Usage:
    python3 diagnose_compose_scan.py [directory]

If no directory is given, uses COMPOSE_DIR from the environment, or /compose.

No dependencies beyond the standard library - safe to drop into any Python 3
environment, including the agent's container or Dockyard's own.
"""

import glob
import os
import re
import sys

SERVICES_KEY_RE = re.compile(r"^services:\s*$", re.MULTILINE)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("COMPOSE_DIR", "/compose")
    print(f"Checking: {target}\n")

    # --- Step 1: does the path even exist and is it readable? ---
    if not os.path.exists(target):
        print(f"[FAIL] This path does not exist inside this container.")
        print(f"       Check the volume mount - the path your compose files live at on the")
        print(f"       HOST is not automatically the same path INSIDE the container. Whatever")
        print(f"       you put on the right side of the ':' in your volume mount (e.g.")
        print(f"       '/srv/docker:/compose:ro') is what COMPOSE_DIR should point at.")
        return
    if not os.path.isdir(target):
        print(f"[FAIL] This path exists but is not a directory.")
        return
    if not os.access(target, os.R_OK):
        print(f"[FAIL] This directory exists but isn't readable by this process (permissions).")
        return
    print(f"[OK] Directory exists and is readable.\n")

    # --- Step 2: full recursive listing, so you can see exactly what's there ---
    print("Full directory tree (following symlinks):")
    total_files = 0
    for root, dirs, filenames in os.walk(target, followlinks=True):
        depth = root[len(target):].count(os.sep)
        indent = "  " * depth
        print(f"{indent}{os.path.basename(root) or root}/")
        for fn in sorted(filenames):
            print(f"{indent}  {fn}")
            total_files += 1
        if depth > 6:
            dirs[:] = []  # don't go infinitely deep in a runaway tree
    if total_files == 0:
        print("  (empty - nothing found at any depth)")
        print("\n[LIKELY CAUSE] The directory is empty from inside this container. This")
        print("  usually means the volume mount is wrong or pointing at the wrong host path,")
        print("  not a problem with Dockyard's scanning logic itself.")
        return
    print()

    # --- Step 3: run the actual glob patterns Dockyard/the agent use ---
    print("Files matching *.yml / *.yaml (recursive, including hidden dirs):")
    patterns = ["**/*.yml", "**/*.yaml"]
    matched = set()
    for p in patterns:
        matched.update(glob.glob(os.path.join(target, p), recursive=True, include_hidden=True))
    if not matched:
        print("  (none found)")
    for path in sorted(matched):
        print(f"  {path}")
    print()

    # --- Step 4: which of those actually look like compose files? ---
    print("Of those, files with a top-level 'services:' key (these are what get used):")
    any_compose = False
    for path in sorted(matched):
        try:
            with open(path, "r") as f:
                content = f.read()
        except Exception as e:
            print(f"  {path}  <- couldn't read: {e}")
            continue
        if SERVICES_KEY_RE.search(content):
            print(f"  {path}  <- OK, will be used")
            any_compose = True
        else:
            print(f"  {path}  <- no top-level 'services:' key found, skipped")

    if not any_compose:
        print("\n[LIKELY CAUSE] No .yml/.yaml file here has a top-level 'services:' key.")
        print("  Check that the key is literally 'services:' at the start of a line (not")
        print("  indented under something else), and that the file itself is valid YAML.")

    print("\nIf everything above looks correct but containers still don't show compose")
    print("data in Dockyard, double check DOCKYARD_HOST_NAME (agent) or a host's \"name\"")
    print("in DOCKYARD_HOSTS (local compose_dir) matches exactly - a mismatch there means")
    print("the data lands under a different host than the one you're looking at.")


if __name__ == "__main__":
    main()
