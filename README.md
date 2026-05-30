Note: this project is mostly vibe-coded and I'm sure there's room for improvement, but in particular the Python merging script for Xaero map files is one of a kind as far as I know so I wanted to publish it.

# Xaero MapSync

Syncs [Xaero's World Map](https://modrinth.com/mod/xaeros-world-map) region files across multiple players on the same Minecraft server. One player runs the sync server; everyone else runs the client. Explored chunks are merged additively — no player's data can overwrite another's.

## How it works

```
Player A ──upload──► Server (golden copy) ──download──► Player B
Player B ──upload──► Server (golden copy) ──download──► Player A
```

1. Each client builds a manifest (SHA256 + mtime) of its local map files
2. The server compares it against the golden copy and returns a diff
3. The client uploads files the server is missing or has stale data for
4. The server merges uploads additively — chunks the golden already has are never overwritten
5. The client downloads the merged golden copy, getting everyone else's exploration

A companion Fabric mod (not in this repo) is signalled to pause Xaero's file writer during downloads, then reload from disk when done.

## Requirements

- Python 3.10+
- The server machine needs a port forwarded and reachable by all players (default: `8765`)
- [Xaero's World Map](https://modrinth.com/mod/xaeros-world-map) installed in Minecraft

## Setup

### Both machines

```bash
git clone https://github.com/YOUR_USERNAME/mapsync.git
cd mapsync
python -m venv venv

# Windows
venv\Scripts\activate

# Linux / macOS
source venv/bin/activate

pip install -r server/requirements.txt
```

---

### Server

Copy and edit the server config:

```bash
cp server/server.env.example server/server.env
```

| Setting | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address. Use `0.0.0.0` for internet-accessible, `127.0.0.1` for local testing. |
| `PORT` | `8765` | Port to listen on. Must be port-forwarded on your router. |
| `GOLDEN_COPY_DIR` | `./golden` | Where the canonical merged map data is stored. |
| `BACKUP_DIR` | `./golden_backups` | Timestamped backups of the golden copy, taken on each startup. |
| `MAX_BACKUPS` | `10` | How many golden backups to keep before pruning old ones. |

Start the server:

```bash
cd server
python server.py
```

On first start the server backfills SHA256 sidecar files for any existing golden regions — this is a one-time slow operation. Subsequent starts and all manifest requests are fast.

---

### Client

Copy and edit the client config:

```bash
cp client/client.env.example client/client.env
```

| Setting | Default | Description |
|---|---|---|
| `SERVER_URL` | — | Full URL of the sync server, e.g. `http://yourserver.ddns.net:8765` |
| `PLAYER_ID` | — | Your Minecraft username. Labels your uploads in the server log. |
| `MAP_DIR` | — | Path to the Xaero world-map folder for the server you want to sync. See below. |
| `SYNC_INTERVAL_MINUTES` | `30` | How often to run a full sync cycle. First sync runs immediately on startup. |
| `BACKUP_DIR` | `./backups` | Local backups of your map folder, taken before every sync cycle. |
| `MAX_BACKUPS` | `5` | How many local backups to keep. |
| `MOD_RELOAD_PORT` | `25566` | Port the companion Fabric mod listens on for pause/reload commands. |
| `MANIFEST_TIMEOUT_SECONDS` | `90` | Seconds to wait for the server to respond to a manifest request. |

**Finding your `MAP_DIR`:**

```
# Windows
C:\Users\<you>\AppData\Roaming\.minecraft\xaero\world-map\Multiplayer_yourserver.net

# Linux / macOS
~/.minecraft/xaero/world-map/Multiplayer_yourserver.net
```

The folder name matches the server address as it appears in your Minecraft server list.

Start the client:

```bash
cd client
python client.py
```

---

## Merge behaviour

The server maintains a **golden copy** — the union of all players' explored chunks. The merge is strictly additive:

- A client uploads a region → the server copies in any chunks the golden doesn't already have
- The golden copy can only grow; no single upload can remove or overwrite existing data
- After merging, the client downloads the updated golden so it gains everyone else's exploration

This means if two players have explored the same chunk differently, the version that arrived at the server first wins for that chunk. The other player's version of that chunk is discarded on upload but received back on download.

## File layout

```
server/
  server.py          — FastAPI sync server
  merge_regions.py   — Binary region file parser and additive merge logic
  server.env         — Server config (gitignored; copy from server.env.example)
  golden/            — Golden copy of all region files (gitignored)
  golden_backups/    — Timestamped golden backups (gitignored)

client/
  client.py          — Sync client with manifest cache and mod socket support
  client.env         — Client config (gitignored; copy from client.env.example)
  backups/           — Local map backups (gitignored)
  manifest_cache.json — SHA256 cache so unchanged files aren't re-hashed (gitignored)
```

## Performance

With large maps (2000+ region files), building a full manifest by hashing every file on every sync was the main bottleneck.

- **Server**: each region file has a `.zip.sha256` sidecar written at upload time. `build_server_manifest()` reads 64-byte sidecars instead of hashing zip files — scales to any number of regions with negligible I/O.
- **Client**: `manifest_cache.json` maps each region file to its last-known SHA256 and filesystem mtime. On each sync only files whose mtime changed are re-hashed. After a normal play session this is typically a few dozen files, not thousands.

## Backup and recovery

- The server takes a full backup of the golden directory on each startup, keeping the last `MAX_BACKUPS` snapshots.
- The client takes a full backup of the local map folder before every sync cycle.
- To roll back, stop the server, replace `golden/` with a backup folder, and restart.
