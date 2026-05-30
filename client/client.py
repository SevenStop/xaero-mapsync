#!/usr/bin/env python3
"""
Xaero MapSync - Client
Periodically syncs the local Xaero map folder with the central server.
Signals the companion Fabric mod to pause writes before downloading and
reload from disk once new files are in place.

Run with: python client.py
Requires:  pip install requests python-dotenv
"""

import hashlib
import json
import os
import shutil
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Dict

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv("client.env")

SERVER_URL            = os.getenv("SERVER_URL",            "http://localhost:8765")
PLAYER_ID             = os.getenv("PLAYER_ID",             "Player")
MAP_DIR               = Path(os.getenv("MAP_DIR",          ""))
SYNC_INTERVAL_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "30"))
BACKUP_DIR            = Path(os.getenv("BACKUP_DIR",       "./backups"))
MAX_BACKUPS           = int(os.getenv("MAX_BACKUPS",        "5"))
MOD_RELOAD_PORT       = int(os.getenv("MOD_RELOAD_PORT",   "25566"))
MANIFEST_TIMEOUT      = int(os.getenv("MANIFEST_TIMEOUT_SECONDS", "90"))
MANIFEST_CACHE_PATH   = Path(os.getenv("MANIFEST_CACHE",   "./manifest_cache.json"))

# ---------------------------------------------------------------------------
# Manifest SHA256 cache — persisted to disk so hashing is skipped for
# files whose filesystem mtime hasn't changed since the last sync.
# ---------------------------------------------------------------------------

_manifest_cache: Dict[str, Dict] = {}


def load_manifest_cache():
    global _manifest_cache
    if MANIFEST_CACHE_PATH.exists():
        try:
            _manifest_cache = json.loads(MANIFEST_CACHE_PATH.read_text())
        except Exception:
            _manifest_cache = {}


def save_manifest_cache():
    try:
        MANIFEST_CACHE_PATH.write_text(json.dumps(_manifest_cache))
    except Exception as e:
        print(f"[Client] WARNING: could not save manifest cache: {e}")

# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup(map_dir: Path, backup_dir: Path, max_backups: int):
    """
    Create a timestamped folder snapshot of the entire map directory.
    Prune oldest snapshots beyond max_backups.
    Called BEFORE any upload — guarantees a rollback point every sync cycle.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"backup_{timestamp}"

    shutil.copytree(map_dir, backup_path)
    print(f"[Client] Backup created: {backup_path}")

    # Prune oldest backups beyond MAX_BACKUPS
    existing = sorted(
        [p for p in backup_dir.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
    )
    while len(existing) > max_backups:
        oldest = existing.pop(0)
        shutil.rmtree(oldest)
        print(f"[Client] Pruned old backup: {oldest.name}")

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(map_dir: Path) -> Dict[str, Dict]:
    """
    Walk map_dir and build { rel_path: { mtime: float, sha256: str } }
    for every .zip file. SHA256 is read from _manifest_cache when the
    file's filesystem mtime matches, avoiding a full re-hash each cycle.
    """
    files = {}
    for zip_path in map_dir.rglob("*.zip"):
        rel      = zip_path.relative_to(map_dir).as_posix()
        fs_mtime = zip_path.stat().st_mtime
        cached   = _manifest_cache.get(rel)
        if cached and cached.get("fs_mtime") == fs_mtime:
            sha256 = cached["sha256"]
        else:
            sha256 = sha256_of_file(zip_path)
            _manifest_cache[rel] = {"sha256": sha256, "fs_mtime": fs_mtime}
        files[rel] = {"mtime": fs_mtime, "sha256": sha256}
    return files

# ---------------------------------------------------------------------------
# Mod socket communication
# ---------------------------------------------------------------------------

def _send_mod_command(command: str) -> bool:
    """
    Send a single-line command to the Fabric mod socket.
    Returns True if the mod replied "ok", False if not running or errored.
    Failure is always non-fatal — logged and ignored.
    """
    try:
        with socket.create_connection(("localhost", MOD_RELOAD_PORT), timeout=5) as s:
            s.sendall((command + "\n").encode())
            response = s.recv(64).decode().strip()
            print(f"[Client] Mod ← '{command}' → '{response}'")
            return response == "ok"
    except ConnectionRefusedError:
        print("[Client] Mod socket not available (Minecraft not open or mod not loaded).")
        return False
    except Exception as e:
        print(f"[Client] Mod socket error on '{command}': {e}")
        return False


def mod_pause() -> bool:
    """
    Ask the mod to drain Xaero's pending save queue and pause its file writer.
    Must be called before copying new files to disk.
    Returns True if the pause is active.
    """
    return _send_mod_command("pause")


def mod_reload():
    """
    Ask the mod to clear all in-memory region data and reload from disk.
    Also resumes the writer that was paused by mod_pause().
    """
    _send_mod_command("reload")

# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------

def upload_files(upload_these: list, map_dir: Path) -> set:
    """
    Upload each requested file to the server along with its local mtime.
    The server will merge it into the golden copy (richer chunk count wins).
    Returns the set of rel_paths that were successfully uploaded.
    """
    succeeded = set()
    for rel_path in upload_these:
        local_path = map_dir / Path(rel_path)
        if not local_path.exists():
            print(f"[Client] Upload skipped (file missing locally): {rel_path}")
            continue
        mtime = local_path.stat().st_mtime
        try:
            with open(local_path, "rb") as f:
                resp = requests.post(
                    f"{SERVER_URL}/upload/{rel_path}",
                    files={"file": (local_path.name, f, "application/zip")},
                    data={"mtime": str(mtime)},
                    timeout=120,
                )
            resp.raise_for_status()
            action = resp.json().get("action", "?")
            print(f"[Client] Uploaded ({action}): {rel_path}")
            succeeded.add(rel_path)
        except Exception as e:
            print(f"[Client] Upload failed for {rel_path}: {e}")
    return succeeded


def download_files(download_these: list, map_dir: Path) -> int:
    """
    Download each requested file from the server, overwriting local copies.
    For files the client uploaded, this fetches the merged golden result —
    the union of the client's exploration and the server's existing data.
    Returns the number of successful downloads.
    """
    succeeded = 0
    for rel_path in download_these:
        local_path = map_dir / Path(rel_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = requests.get(
                f"{SERVER_URL}/download/{rel_path}",
                timeout=120,
                stream=True,
            )
            resp.raise_for_status()
            h = hashlib.sha256()
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    h.update(chunk)
            _manifest_cache[rel_path] = {
                "sha256":   h.hexdigest(),
                "fs_mtime": local_path.stat().st_mtime,
            }
            print(f"[Client] Downloaded: {rel_path}")
            succeeded += 1
        except Exception as e:
            print(f"[Client] Download failed for {rel_path}: {e}")
    return succeeded

# ---------------------------------------------------------------------------
# Core sync cycle
# ---------------------------------------------------------------------------

def sync():
    """
    Full sync cycle. Steps:
      1. Backup local map folder (before touching anything)
      2. Build local manifest
      3. POST manifest → get upload_these / download_these diff
      4. Upload our files to server (server merges into golden copy)
      5. Pause Xaero's file writer via mod socket
      6. Download updated/new/merged files from server
      7. Signal mod to reload from disk (also resumes writer)
    """
    print(f"\n[Client] ── Sync starting {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ──")

    if not MAP_DIR or not MAP_DIR.exists():
        print(f"[Client] ERROR: MAP_DIR not found: {MAP_DIR}")
        print("[Client] Check MAP_DIR in client.env.")
        return

    # ── Step 1: Backup ────────────────────────────────────────────────────────
    try:
        backup(MAP_DIR, BACKUP_DIR, MAX_BACKUPS)
    except Exception as e:
        print(f"[Client] Backup failed: {e}")
        print("[Client] Aborting sync — not safe to proceed without a backup.")
        return

    # ── Step 2: Build manifest ────────────────────────────────────────────────
    print("[Client] Building local manifest...")
    manifest = build_manifest(MAP_DIR)
    print(f"[Client] {len(manifest)} local region file(s) found.")

    # ── Step 3: Get diff from server ──────────────────────────────────────────
    try:
        resp = requests.post(
            f"{SERVER_URL}/manifest",
            json={"player_id": PLAYER_ID, "files": manifest},
            timeout=MANIFEST_TIMEOUT,
        )
        resp.raise_for_status()
        diff = resp.json()
    except Exception as e:
        print(f"[Client] Could not reach server for manifest diff: {e}")
        return

    upload_these   = diff.get("upload_these",   [])
    download_these = diff.get("download_these", [])
    print(f"[Client] Diff → upload: {len(upload_these)}, download: {len(download_these)}")

    if not upload_these and not download_these:
        print("[Client] Already in sync — nothing to do.")
        return

    # ── Step 4: Upload first (never lose local exploration) ───────────────────
    upload_set  = set(upload_these)
    uploaded_ok = set()
    if upload_these:
        print(f"[Client] Uploading {len(upload_these)} file(s)...")
        uploaded_ok = upload_files(upload_these, MAP_DIR)
        print(f"[Client] Uploaded {len(uploaded_ok)}/{len(upload_these)} file(s).")

    # ── Step 4b: Second manifest pass ─────────────────────────────────────────
    # Other clients may have uploaded and merged files while we were uploading.
    # Re-request the manifest so we catch those concurrent merges.  We only add
    # pure downloads here (no re-uploads) to avoid an infinite loop.
    if uploaded_ok:
        try:
            fresh_manifest = build_manifest(MAP_DIR)
            resp2 = requests.post(
                f"{SERVER_URL}/manifest",
                json={"player_id": PLAYER_ID, "files": fresh_manifest},
                timeout=MANIFEST_TIMEOUT,
            )
            resp2.raise_for_status()
            diff2 = resp2.json()
            extra = [
                p for p in diff2.get("download_these", [])
                if p not in set(download_these) and p not in upload_set
            ]
            if extra:
                print(f"[Client] {len(extra)} extra file(s) merged concurrently — queuing download.")
                download_these = download_these + extra
        except Exception as e:
            print(f"[Client] Second manifest check failed (non-fatal): {e}")

    # Only download a file if it is server-only OR its upload succeeded.
    # Downloading after a failed upload would overwrite local unique exploration
    # data with the pre-merge golden, permanently losing that data.
    safe_to_download = [
        p for p in download_these
        if p not in upload_set or p in uploaded_ok
    ]

    # ── Step 5 & 6 & 7: Pause → download → reload ────────────────────────────
    if safe_to_download:
        mod_active = mod_pause()
        if not mod_active:
            print("[Client] Mod not active — files will be downloaded but the map")
            print("[Client] will not reload automatically. Relog to see changes.")

        print(f"[Client] Downloading {len(safe_to_download)} file(s)...")
        dl_ok = download_files(safe_to_download, MAP_DIR)
        print(f"[Client] Downloaded {dl_ok}/{len(safe_to_download)} file(s).")

        if mod_active:
            mod_reload()

    save_manifest_cache()
    print(f"[Client] ── Sync complete ─────────────────────────────────────────────\n")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  Xaero MapSync Client")
    print("=" * 60)
    print(f"  Player    : {PLAYER_ID}")
    print(f"  Server    : {SERVER_URL}")
    print(f"  Map dir   : {MAP_DIR}")
    print(f"  Interval  : {SYNC_INTERVAL_MINUTES} min")
    print(f"  Backup dir: {BACKUP_DIR}")
    print(f"  Max backups: {MAX_BACKUPS}")
    print(f"  Mod port  : {MOD_RELOAD_PORT}")
    print(f"  Manifest timeout: {MANIFEST_TIMEOUT}s")
    print("=" * 60)

    load_manifest_cache()

    if not MAP_DIR or str(MAP_DIR) == ".":
        print("[Client] FATAL: MAP_DIR is not set in client.env. Cannot continue.")
        return

    # Quick health check on startup
    try:
        r = requests.get(f"{SERVER_URL}/health", timeout=5)
        r.raise_for_status()
        info = r.json()
        print(f"[Client] Server reachable — golden regions: {info.get('golden_regions', '?')}, "
              f"backups: {info.get('backups', '?')}")
    except Exception as e:
        print(f"[Client] WARNING: Could not reach server ({e}). Will retry on first sync.")

    # Sync immediately on startup, then on the configured interval
    sync()
    while True:
        print(f"[Client] Next sync in {SYNC_INTERVAL_MINUTES} minute(s)...")
        time.sleep(SYNC_INTERVAL_MINUTES * 60)
        sync()


if __name__ == "__main__":
    main()