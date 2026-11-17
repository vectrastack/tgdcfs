---
sidebar_position: 5
title: Redundancy, Mirroring & Disaster Recovery
description: Multi-channel RAID-1 style store redundancy, sync modes, replicas, and promotion runbook.
---

# 🔄 Redundancy, Mirroring & Disaster Recovery

TGDCFS provides RAID-1 style channel redundancy. A single **File System** can mirror data across multiple Telegram and Discord channels.

---

## Aligned Copies vs Replicas

When mirroring files between stores, TGDCFS handles differences in part limits automatically:

- **Aligned Copies**: Used when the mirror is in the same backend as the primary, or when every part of the primary fits in one message of the mirror (e.g., Telegram to Telegram, or a Discord primary to a Telegram mirror). Every message in the primary store maps 1-to-1 with a message in the mirror store (`mirrors` map).
- **Replicas**: Used when the mirror is in another backend and the primary's parts do not fit one of its messages (e.g., a 2 GiB Telegram primary mirrored to a 20 MB Discord channel). TGDCFS cuts the file into Discord-sized pieces and tracks them in a compact `replicas` map.

---

## Replication Copy Modes (`mode`)

Configured per file system under `filesystems.<fs>.mode`:

| Mode | Behaviour | Bandwidth Cost |
|---|---|---|
| `auto` *(default)* | Attempts server-side message forwarding (`copy_from`). Falls back to streaming reupload if forwarding is unsupported or blocked. | Zero bandwidth for Telegram-to-Telegram; streaming for cross-backend. |
| `forward` | Forces server-side message forwarding (`forwardMessages`). Fails if channel has "Restrict saving content". | Zero bandwidth. |
| `reupload` | Always re-downloads and streams uploaded bytes to target mirror. | Requires double bandwidth. |

---

## Replication Sync Modes (`sync`)

Configured per file system under `filesystems.<fs>.sync`:

### 1. `inline`
- Client PUT request blocks until upload completes on primary store AND all mirror stores.
- Required if `strict: true` is set.

### 2. `background` *(default for cross-backend/reupload)*
- Client PUT request receives `201 Created` as soon as primary store write succeeds.
- Reupload tasks are added to the persistent background queue (`replication_queue.json`) and processed asynchronously by one background worker per file system, with a retry backoff after failures.

### 3. `tee`
- Bytes read from the incoming client stream are duplicated in memory to bounded branch buffers (`tee_buffer_mb`, default 64 MB).
- Uploads to primary and reuploading mirrors run concurrently in parallel during the client PUT request. No local disk cache required.

---

## Failover Reads

When a read from the primary fails, the primary is considered dead for 60 seconds (`primary_dead` on `/api/filesystems`) and reads prefer the mirrors in that window.

If a message chunk or store becomes unreachable (e.g. FloodWait rate limit or network issue):
- TGDCFS automatically fails over to available mirror stores or replicas chunk-by-chunk.
- Read operations resume seamlessly at the exact byte where the failure occurred.

---

## Metadata Redundancy

The files themselves already had this: the primary store and its mirrors each hold a copy, so one failing store never loses data. The metadata -- the directory tree, file references and stats -- can now have the same redundancy, configured as a list under `filesystems.<fs>.metadata`:

```yaml
filesystems:
  media:
    primary: tg-primary
    mirrors:
      - dc-mirror
    metadata:
      - type: pinned_message
        store: tg-primary   # a Telegram pinned message
      - type: pinned_message
        store: dc-mirror    # a Discord pinned message
      - type: github_repo
        github_repo: { repo: "you/tgdcfs-metadata", commit: "master", access_token: "..." }
      - type: gitlab_repo
        gitlab_repo: { repo: "group/tgdcfs-metadata", branch: "main", access_token: "..." }
```

How it behaves:

- **Reads fail over.** The first entry that can be read at boot is the tree of record. If an entry is unreachable, it is skipped and the next one answers.
- **Writes fan out.** Every change is written to every entry. A single mapping instead of a list (the historical form) keeps working as a one-entry set.
- **Outages are absorbed.** An entry that fails a write is marked stale and the file system keeps running on the others; the stale entry is caught up from the tree of record on the next write and by a periodic resync (every 5 minutes), so a store that comes back is healed without any manual action.
- **Pinned messages are per store.** A `pinned_message` entry names its `store` (the primary by default, any of its mirrors otherwise), so a Telegram pinned message and a Discord pinned message are configured -- and treated -- as two separate, independently failing copies. A store with its own pinned entry is skipped by the automatic promotion copy of the pinned message.
- **`github_repo` and `gitlab_repo`** each hold the tree as a whole (a git history of per-directory commits, and one JSON file on a branch respectively). A GitHub repo that missed writes during an outage is rebuilt from the tree of record in a single commit, after which writes commit to it directly again.

The state of every entry is visible on the management API: `GET /api/filesystems` reports the configured `metadata` list and each entry's `active`/`stale` health.

---

## Disaster Recovery / Promotion Runbook

If a primary store channel is banned, deleted, or permanently lost, follow this step-by-step promotion runbook to swap a mirror store into the primary role:

### Step 1: Stop TGDCFS Service
```bash
docker stop tgdcfs  # or sudo systemctl stop tgdcfs
```

### Step 2: Edit `config.yaml`
In `config.yaml`, swap store roles under `filesystems`:

```yaml
# BEFORE
filesystems:
  media:
    primary: tg-main
    mirrors:
      - tg-backup

# AFTER
filesystems:
  media:
    primary: tg-backup
    mirrors: []  # or keep tg-main if temporarily disabled
```

### Step 3: Restart TGDCFS
```bash
docker start tgdcfs  # or sudo systemctl start tgdcfs
```
*Note: there is no relocation step on boot. When a file's descriptor is read, TGDCFS re-expresses it relative to the new primary, so files remain readable from `tg-backup` right away; the content copies are made by the backfill in the next step. This requires that `tg-backup` holds a copy of the metadata: with `pinned_message` metadata a mirror whose messages are too small for the metadata blob (a Discord mirror of a Telegram primary) has none, and promotion then needs `github_repo` metadata.*

### Step 4: Execute Backfill Task
Trigger backfill via curl to ensure all version descriptors are updated and copies are promoted:

```bash
curl -X POST http://localhost:1900/api/redundancy/backfill/media
```

Check task progress:
```bash
curl http://localhost:1900/api/tasks
```

### Step 5: Restore Redundancy
Add a fresh backup channel to `mirrors` in `config.yaml` and run backfill once more to re-establish RAID-1 redundancy!
