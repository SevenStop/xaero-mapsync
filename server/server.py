#!/usr/bin/env python3
"""
Xaero MapSync - Server
Maintains a golden copy of all region files, merges uploads, serves downloads.
Backs up the golden directory before each sync session begins.

Run with: python server.py
Requires:  pip install fastapi uvicorn python-dotenv
Also requires merge_regions.py in the same directory (from the region reverse-engineering script).
"""

import hashlib
import os
import shutil
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv("server.env")

GOLDEN_DIR  = Path(os.getenv("GOLDEN_COPY_DIR", "./golden"))
BACKUP_DIR  = Path(os.getenv("BACKUP_DIR",      "./golden_backups"))
HOST        = os.getenv("HOST", "0.0.0.0")
PORT        = int(os.getenv("PORT", "8765"))
MAX_BACKUPS = int(os.getenv("MAX_BACKUPS", "10"))

GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Import merge functions from the reverse-engineered script
# ---------------------------------------------------------------------------

try:
    from merge_regions import parse_region, merge_copy_additive, write_region
    MERGE_AVAILABLE = True
except ImportError:
    print("[Server] WARNING: merge_regions.py not found. Uploads will overwrite instead of merge.")
    MERGE_AVAILABLE = False

# ---------------------------------------------------------------------------
# Startup backup of the golden directory
# ---------------------------------------------------------------------------

def backup_golden():
    """
    Take a timestamped snapshot of the entire golden directory on startup.
    Prunes old backups beyond MAX_BACKUPS.
    """
    if not any(GOLDEN_DIR.rglob("*.zip")):
        print("[Server] Golden directory is empty — skipping startup backup.")
        return

    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"golden_{timestamp}"
    shutil.copytree(GOLDEN_DIR, backup_path)
    print(f"[Server] Golden backup created: {backup_path}")

    # Prune oldest backups beyond MAX_BACKUPS
    existing = sorted(
        [p for p in BACKUP_DIR.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
    )
    while len(existing) > MAX_BACKUPS:
        oldest = existing.pop(0)
        shutil.rmtree(oldest)
        print(f"[Server] Pruned old golden backup: {oldest.name}")

# ---------------------------------------------------------------------------
# Per-file locking — uploads for the same region are serialised,
# uploads for different regions proceed in parallel.
# ---------------------------------------------------------------------------

_file_locks: Dict[str, threading.Lock] = {}
_locks_mutex = threading.Lock()

def get_file_lock(rel_path: str) -> threading.Lock:
    with _locks_mutex:
        if rel_path not in _file_locks:
            _file_locks[rel_path] = threading.Lock()
        return _file_locks[rel_path]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def golden_path(rel_path: str) -> Path:
    """Absolute path to the golden copy of a region file."""
    p = GOLDEN_DIR / Path(rel_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def mtime_file(rel_path: str) -> Path:
    """Sidecar .mtime file storing the canonical mtime for a golden region."""
    return golden_path(rel_path).with_suffix(".zip.mtime")

def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def get_golden_mtime(rel_path: str) -> Optional[float]:
    mf = mtime_file(rel_path)
    if mf.exists():
        return float(mf.read_text().strip())
    gp = golden_path(rel_path)
    return gp.stat().st_mtime if gp.exists() else None

def set_golden_mtime(rel_path: str, mtime: float):
    mtime_file(rel_path).write_text(str(mtime))

def sha256_sidecar_path(rel_path: str) -> Path:
    return golden_path(rel_path).with_suffix(".zip.sha256")

def read_sha256_sidecar(rel_path: str) -> Optional[str]:
    p = sha256_sidecar_path(rel_path)
    return p.read_text().strip() if p.exists() else None

def write_sha256_sidecar(rel_path: str, sha256_hex: str):
    sha256_sidecar_path(rel_path).write_text(sha256_hex)

def merge_zips(golden_path: str, upload_path: str, output_path: str):
    """Merge an upload into the golden copy additively.
    Only chunk positions the golden has never seen are copied from the upload;
    existing golden chunks are never overwritten."""
    with zipfile.ZipFile(golden_path) as zf:
        golden_data = zf.read("region.xaero")
    with zipfile.ZipFile(upload_path) as zf:
        upload_data = zf.read("region.xaero")
    base  = parse_region(golden_data)
    patch = parse_region(upload_data)
    merge_copy_additive(base, patch)
    write_region(base, output_path)

# ---------------------------------------------------------------------------
# Backfill SHA256 sidecars for golden files that predate this feature
# ---------------------------------------------------------------------------

def backfill_sha256_sidecars():
    missing = [
        gp for gp in GOLDEN_DIR.rglob("*.zip")
        if not sha256_sidecar_path(gp.relative_to(GOLDEN_DIR).as_posix()).exists()
    ]
    if not missing:
        print("[Server] SHA256 sidecars: all present.")
        return
    print(f"[Server] Backfilling SHA256 sidecars for {len(missing)} file(s)...")
    for gp in missing:
        rel = gp.relative_to(GOLDEN_DIR).as_posix()
        write_sha256_sidecar(rel, sha256_of_file(gp))
    print("[Server] SHA256 backfill complete.")


# ---------------------------------------------------------------------------
# Build server manifest
# ---------------------------------------------------------------------------

def build_server_manifest() -> Dict[str, Dict]:
    files = {}
    for gp in GOLDEN_DIR.rglob("*.zip"):
        rel    = gp.relative_to(GOLDEN_DIR).as_posix()
        sha256 = read_sha256_sidecar(rel)
        if sha256 is None:
            sha256 = sha256_of_file(gp)
            write_sha256_sidecar(rel, sha256)
        files[rel] = {
            "mtime":  get_golden_mtime(rel) or gp.stat().st_mtime,
            "sha256": sha256,
        }
    return files

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Xaero MapSync Server")


@app.post("/manifest")
def compare_manifest(payload: dict):
    """
    Client POSTs its manifest: {player_id, files: {rel_path: {mtime, sha256}}}
    Server returns:
      upload_these   — files the client should upload (content differs, or server missing)
      download_these — files the client should download (content differs, or client missing)

    Hash-identical files are skipped entirely — no transfer needed.

    When both sides have the same file with different content, the client always
    uploads AND downloads: upload so the server can merge unique chunks from both
    players (server uses mtime to resolve conflicts), then download the merged
    golden copy so the client gets the other player's exploration too.
    """
    player_id    = payload.get("player_id", "unknown")
    client_files = payload.get("files", {})

    print(f"[Server] Manifest from {player_id}: {len(client_files)} files")

    server_files = build_server_manifest()
    all_paths    = set(client_files) | set(server_files)

    upload_these   = []
    download_these = []

    for rel_path in all_paths:
        c = client_files.get(rel_path)
        s = server_files.get(rel_path)

        if c and s:
            if c["sha256"] == s["sha256"]:
                continue                          # identical — skip entirely
            # Both sides have the file but with different content: always upload
            # so the server can merge unique chunks from both players. The server
            # uses mtime to resolve conflicts (newer data wins), but mtime alone
            # cannot tell whether the client has chunks the server has never seen.
            upload_these.append(rel_path)
            download_these.append(rel_path)
        elif c and not s:
            upload_these.append(rel_path)         # server missing — upload only
        elif s and not c:
            download_these.append(rel_path)       # client missing — download only

    print(f"[Server]   → upload: {len(upload_these)}, download: {len(download_these)}")
    return {"upload_these": upload_these, "download_these": download_these}


@app.post("/upload/{rel_path:path}")
async def upload_file(
    rel_path: str,
    file: UploadFile = File(...),
    mtime: float = Form(...),
):
    """
    Client uploads a zip with its original local mtime.
    Server merges the upload into the golden copy (newer mtime = patch).
    """
    lock    = get_file_lock(rel_path)
    content = await file.read()

    with lock:
        gp = golden_path(rel_path)

        # Write upload to a temp file first
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(content)

            # New region — server has never seen it
            if not gp.exists():
                shutil.move(tmp_path, gp)
                set_golden_mtime(rel_path, mtime)
                write_sha256_sidecar(rel_path, hashlib.sha256(content).hexdigest())
                print(f"[Server] Stored new region: {rel_path}")
                return {"status": "ok", "action": "stored"}

            # Identical to golden copy — nothing to do
            upload_hash  = hashlib.sha256(content).hexdigest()
            golden_hash  = read_sha256_sidecar(rel_path) or sha256_of_file(gp)
            if upload_hash == golden_hash:
                os.unlink(tmp_path)
                print(f"[Server] Skipped identical: {rel_path}")
                return {"status": "ok", "action": "skipped_identical"}

            # No merge script — fall back to newest-wins overwrite
            if not MERGE_AVAILABLE:
                golden_mtime = get_golden_mtime(rel_path) or gp.stat().st_mtime
                if mtime >= golden_mtime:
                    shutil.move(tmp_path, gp)
                    set_golden_mtime(rel_path, mtime)
                    write_sha256_sidecar(rel_path, upload_hash)
                    print(f"[Server] Overwrote (no merge script): {rel_path}")
                else:
                    os.unlink(tmp_path)
                    print(f"[Server] Kept existing (no merge script): {rel_path}")
                return {"status": "ok", "action": "overwrite_no_merge"}

            # Merge additively: golden is always the base, upload only adds
            # chunk positions the golden has never seen.  The golden's existing
            # chunks are never overwritten, so accumulated merged data from all
            # players can only grow, never be degraded by a single upload.
            golden_mtime = get_golden_mtime(rel_path) or gp.stat().st_mtime
            result_mtime = max(mtime, golden_mtime)

            # Merge to a temp output, then atomically replace the golden copy
            out_fd, out_path = tempfile.mkstemp(suffix=".zip")
            os.close(out_fd)
            try:
                merge_zips(str(gp), tmp_path, out_path)
                merged_sha256 = sha256_of_file(Path(out_path))
                shutil.move(out_path, gp)
                set_golden_mtime(rel_path, result_mtime)
                write_sha256_sidecar(rel_path, merged_sha256)
                print(f"[Server] Merged: {rel_path}")
            except Exception:
                if os.path.exists(out_path):
                    os.unlink(out_path)
                raise

        except Exception as e:
            print(f"[Server] ERROR processing {rel_path}: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    return {"status": "ok", "action": "merged"}


@app.get("/download/{rel_path:path}")
def download_file(rel_path: str):
    """Serve a golden copy region file to a client."""
    lock = get_file_lock(rel_path)
    with lock:
        gp = golden_path(rel_path)
        if not gp.exists():
            raise HTTPException(status_code=404, detail="Region not found")
        data = gp.read_bytes()
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{Path(rel_path).name}"'},
    )


@app.get("/health")
def health():
    regions = sum(1 for _ in GOLDEN_DIR.rglob("*.zip"))
    backups = sum(1 for _ in BACKUP_DIR.iterdir() if _.is_dir()) if BACKUP_DIR.exists() else 0
    return {"status": "ok", "golden_regions": regions, "backups": backups}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"[Server] Xaero MapSync server starting on {HOST}:{PORT}")
    print(f"[Server] Golden copy dir : {GOLDEN_DIR.resolve()}")
    print(f"[Server] Backup dir      : {BACKUP_DIR.resolve()}")
    print(f"[Server] Max backups     : {MAX_BACKUPS}")
    print(f"[Server] Merge available : {MERGE_AVAILABLE}")
    backup_golden()
    backfill_sha256_sidecars()
    uvicorn.run(app, host=HOST, port=PORT)