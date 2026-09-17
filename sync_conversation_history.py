#!/usr/bin/env python3
"""Sync and enforce LIFO (newest first, keep latest 100) on Antigravity IDE conversation history index.

This script scans all conversation databases (.db and .pb) stored in
%USERPROFILE%/.gemini/antigravity-ide/conversations/, ensures all recent
conversations are summarized and indexed, and writes the latest 100 conversations
into the IDE's unified state sync storage (state.vscdb).
"""

import argparse
import base64
import datetime
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

DEFAULT_VSCDB = Path(os.environ.get("APPDATA", "")) / "Antigravity IDE/User/globalStorage/state.vscdb"
DEFAULT_CONV_DIR = Path.home() / ".gemini/antigravity-ide/conversations"
DEFAULT_BRAIN_DIR = Path.home() / ".gemini/antigravity-ide/brain"


def encode_varint(val):
    res = bytearray()
    while val > 0x7f:
        res.append((val & 0x7f) | 0x80)
        val >>= 7
    res.append(val & 0x7f)
    return bytes(res)


def decode_varint(data, pos):
    res = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        res |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    return res, pos


def make_tag(field_num, wire_type):
    return encode_varint((field_num << 3) | wire_type)


def make_length_delimited(field_num, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return make_tag(field_num, 2) + encode_varint(len(data)) + data


def make_varint_field(field_num, val):
    return make_tag(field_num, 0) + encode_varint(val)


def make_timestamp(field_num, dt_or_ts):
    if hasattr(dt_or_ts, "timestamp"):
        ts = int(dt_or_ts.timestamp())
        nanos = int(dt_or_ts.microsecond * 1000)
    else:
        ts = int(dt_or_ts)
        nanos = 0
    body = make_varint_field(1, ts)
    if nanos > 0:
        body += make_varint_field(2, nanos)
    return make_tag(field_num, 2) + encode_varint(len(body)) + body


def create_summary_chunk(db_path, brain_dir):
    cid = os.path.basename(db_path).replace(".db", "")
    mtime = os.path.getmtime(db_path)
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    tmeta = cur.execute(
        "SELECT trajectory_id, cascade_id, trajectory_type, source FROM trajectory_meta"
    ).fetchone()
    trajectory_id = tmeta[0] if tmeta else cid
    source = tmeta[3] if tmeta and len(tmeta) > 3 else 1
    trajectory_type = tmeta[2] if tmeta and len(tmeta) > 2 else 4

    step_count = cur.execute("SELECT count(*) FROM steps").fetchone()[0]

    title = None
    for p, in cur.execute(
        "SELECT step_payload FROM steps WHERE step_type = 23 ORDER BY idx DESC"
    ):
        if p:
            m = re.findall(rb"[\x20-\x7e]{6,60}", p)
            for s in m:
                text = s.decode("latin-1").strip()
                if not any(
                    k in text.lower()
                    for k in (
                        "sessionid",
                        "transcript",
                        "cybercomply",
                        "http",
                        "file:",
                        ".json",
                        "users/",
                    )
                ):
                    if len(text) > 8:
                        title = text
                        break
            if title:
                break

    if not title:
        tpath = brain_dir / cid / ".system_generated/logs/transcript.jsonl"
        if tpath.exists():
            try:
                for line in open(tpath, "r", encoding="utf-8", errors="ignore"):
                    if "USER_INPUT" in line:
                        d = json.loads(line)
                        cnt = d.get("content", "")
                        if "<USER_REQUEST>" in cnt:
                            cnt = cnt.split("<USER_REQUEST>")[1].split("</USER_REQUEST>")[0]
                        clean = re.sub(r"@\[[^\]]+\]", "", cnt).strip()
                        if clean:
                            title = clean.replace("\n", " ")[:50]
                        break
            except Exception:
                pass

    if not title:
        title = "Session " + cid[:8]

    created_ts = mtime
    tpath = brain_dir / cid / ".system_generated/logs/transcript.jsonl"
    if tpath.exists():
        try:
            for line in open(tpath, "r", encoding="utf-8", errors="ignore"):
                if "created_at" in line:
                    d = json.loads(line)
                    ca = d.get("created_at")
                    if ca:
                        try:
                            dt = datetime.datetime.fromisoformat(ca.replace("Z", "+00:00"))
                            created_ts = dt.timestamp()
                        except Exception:
                            pass
                    break
        except Exception:
            pass

    tblob = cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
    raw_blob = tblob[0] if tblob else None

    workspace_field = b""
    if raw_blob:
        bp = 0
        while bp < len(raw_blob):
            btag = raw_blob[bp]
            bp += 1
            bfield = btag >> 3
            blen, bp = decode_varint(raw_blob, bp)
            bdata = raw_blob[bp : bp + blen]
            bp += blen
            if bfield == 1:
                workspace_field += make_length_delimited(9, bdata)

    summary_body = (
        make_length_delimited(1, title)
        + make_varint_field(2, step_count)
        + make_timestamp(3, mtime)
        + make_length_delimited(4, trajectory_id)
        + make_varint_field(5, 1)
        + make_timestamp(7, created_ts)
        + workspace_field
        + make_timestamp(10, mtime)
        + make_varint_field(16, 0)
        + (make_length_delimited(17, raw_blob) if raw_blob else b"")
        + make_varint_field(20, source)
        + make_varint_field(22, trajectory_type)
    )

    b64_summary = base64.b64encode(summary_body)
    f2 = make_length_delimited(1, b64_summary)
    chunk = make_length_delimited(1, cid) + make_length_delimited(2, f2)
    return cid, title, mtime, chunk


def sync_conversations(
    vscdb_path=DEFAULT_VSCDB,
    conv_dir=DEFAULT_CONV_DIR,
    brain_dir=DEFAULT_BRAIN_DIR,
    limit=100,
    dry_run=False,
):
    if not vscdb_path.is_file():
        print(f"Error: Database not found at {vscdb_path}", file=sys.stderr)
        return 1

    con = sqlite3.connect(vscdb_path, timeout=10.0)
    cur = con.cursor()
    row = cur.execute(
        "SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.trajectorySummaries'"
    ).fetchone()

    existing_chunks = {}
    if row and row[0]:
        try:
            raw = base64.b64decode(row[0])
            pos = 0
            while pos < len(raw):
                tag = raw[pos]
                pos += 1
                length, pos = decode_varint(raw, pos)
                chunk = raw[pos : pos + length]
                pos += length
                p = 0
                p += 1
                l1, p = decode_varint(chunk, p)
                u = chunk[p : p + l1].decode("utf-8")
                existing_chunks[u] = chunk
        except Exception as e:
            print(f"Warning: Failed to decode existing entries: {e}")

    db_files = list(conv_dir.glob("*.db"))
    pb_files = list(conv_dir.glob("*.pb"))

    all_convs = []
    for f in db_files:
        cid = f.stem
        mtime = f.stat().st_mtime
        if cid in existing_chunks:
            all_convs.append((cid, mtime, existing_chunks[cid], "existing"))
        else:
            try:
                cid, title, mtime, chunk = create_summary_chunk(str(f), brain_dir)
                all_convs.append((cid, mtime, chunk, "generated"))
            except Exception as ex:
                print(f"Warning: Failed to generate chunk for {cid}: {ex}", file=sys.stderr)

    for f in pb_files:
        cid = f.stem
        mtime = f.stat().st_mtime
        if cid in existing_chunks:
            all_convs.append((cid, mtime, existing_chunks[cid], "existing_pb"))

    # Sort LIFO: newest first
    all_convs.sort(key=lambda x: x[1], reverse=True)

    selected = all_convs[:limit]
    print(f"Total conversations on disk: {len(all_convs)}")
    print(f"Selected top {len(selected)} latest conversations (LIFO)")
    if selected:
        print(f"  Newest: {selected[0][0]} ({datetime.datetime.fromtimestamp(selected[0][1])})")
        print(f"  Oldest in window: {selected[-1][0]} ({datetime.datetime.fromtimestamp(selected[-1][1])})")

    # Serialize in ascending order so index displays properly
    new_raw = bytearray()
    for cid, mtime, chunk, source in reversed(selected):
        new_raw.extend(make_tag(1, 2) + encode_varint(len(chunk)) + chunk)

    new_b64 = base64.b64encode(bytes(new_raw)).decode("utf-8")

    if dry_run:
        print("Dry run complete. No changes made.")
        return 0

    backup_path = vscdb_path.with_suffix(".vscdb.backup_lifo")
    if not backup_path.exists():
        shutil.copy2(vscdb_path, backup_path)
        print(f"Created backup: {backup_path}")

    cur.execute(
        "UPDATE ItemTable SET value = ? WHERE key = 'antigravityUnifiedStateSync.trajectorySummaries'",
        (new_b64,),
    )
    con.commit()
    con.close()
    print("Successfully updated state.vscdb with latest 100 conversations!")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vscdb", type=Path, default=DEFAULT_VSCDB, help="Path to state.vscdb")
    parser.add_argument("--conv-dir", type=Path, default=DEFAULT_CONV_DIR, help="Path to conversations dir")
    parser.add_argument("--brain-dir", type=Path, default=DEFAULT_BRAIN_DIR, help="Path to brain dir")
    parser.add_argument("--limit", type=int, default=100, help="Maximum number of conversations to keep (default: 100)")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing to disk")
    args = parser.parse_args()
    return sync_conversations(
        vscdb_path=args.vscdb,
        conv_dir=args.conv_dir,
        brain_dir=args.brain_dir,
        limit=args.limit,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
