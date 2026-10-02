"""OneDrive inbox for Field Notes Transcriber.

Watches  <OneDrive>\\Field Notes Inbox  (and optionally <OneDrive>\\Recordings, where Teams saves meetings
you record). When a recording has finished syncing, it is queued for transcription right away. Field Notes
on this computer then imports it (with its _notes.txt if present) and the originals move to an
"Imported" subfolder. Teams files are never moved; already-existing ones are ignored (only new ones count).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

import engine

MEDIA = {".wav", ".m4a", ".mp3", ".mp4", ".webm", ".ogg", ".mov", ".aac", ".wma", ".flac", ".mkv"}
INBOX_NAME = "Field Notes Inbox"
STABLE_SECONDS = 15


def read_wav_meta(path: Path):
    """Field Notes WAVs carry title, attendees and notes in an extra RIFF chunk 'fnmd' (JSON)."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"RIFF":
                return None
            f.read(4)
            if f.read(4) != b"WAVE":
                return None
            for _ in range(32):
                h = f.read(8)
                if len(h) < 8:
                    return None
                cid, n = h[:4], int.from_bytes(h[4:], "little")
                if cid == b"fnmd":
                    return json.loads(f.read(n).decode("utf-8"))
                f.seek(n + (n % 2), 1)
    except Exception:
        return None
    return None


def find_onedrive() -> Path | None:
    for var in ("OneDriveCommercial", "OneDrive", "OneDriveConsumer"):
        p = os.environ.get(var)
        if p and Path(p).is_dir():
            return Path(p)
    p = Path.home() / "OneDrive"
    return p if p.is_dir() else None


class Inbox:
    def __init__(self, submit, log=print, job_active=lambda jid: False):
        """submit(path, title, attendees, keep_file=True, outputs=...) -> job id; job_active(jid) -> still queued/running"""
        self.submit, self.log, self.job_active = submit, log, job_active
        self.cfg_path = engine.app_dir() / "inbox.json"
        self.state_path = engine.app_dir() / "inbox_state.json"
        self.lock = threading.Lock()
        self.cfg = {"enabled": True, "inbox": None, "watch_teams": False, "auto_transcribe": True}
        self.state = {"items": {}, "baseline": {}}
        try:
            self.cfg.update(json.loads(self.cfg_path.read_text(encoding="utf-8")))
        except Exception:
            pass
        try:
            self.state.update(json.loads(self.state_path.read_text(encoding="utf-8")))
        except Exception:
            pass
        self._sizes: dict[str, tuple] = {}

    # ---- config
    def onedrive(self):
        return find_onedrive()

    def folders(self) -> list[dict]:
        od = self.onedrive()
        inbox = Path(self.cfg["inbox"]) if self.cfg.get("inbox") else (od / INBOX_NAME if od else None)
        out = []
        if inbox:
            out.append({"kind": "inbox", "path": str(inbox), "exists": inbox.is_dir()})
        if od:
            rec = od / "Recordings"
            out.append({"kind": "teams", "path": str(rec), "exists": rec.is_dir(), "on": bool(self.cfg.get("watch_teams"))})
        return out

    def set_config(self, **kw):
        with self.lock:
            for k in ("watch_teams", "auto_transcribe", "enabled"):
                if k in kw:
                    self.cfg[k] = bool(kw[k])
            if "inbox" in kw:
                self.cfg["inbox"] = kw["inbox"] or None
            self.cfg_path.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
            if kw.get("watch_teams"):
                self._baseline_teams()

    def _save(self):
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def _baseline_teams(self):
        """Mark Teams recordings that already exist as seen, so only new ones are picked up."""
        for f in self.folders():
            if f["kind"] == "teams" and f["exists"]:
                base = self.state["baseline"].setdefault(f["path"], [])
                for p in Path(f["path"]).iterdir():
                    if p.suffix.lower() in MEDIA and str(p) not in base:
                        base.append(str(p))
        self._save()

    def transcripts_dir(self) -> Path | None:
        inbox = next((Path(f["path"]) for f in self.folders() if f["kind"] == "inbox"), None)
        return inbox / "Transcripts" if inbox else None

    def write_transcript(self, iid, text: str) -> bool:
        """Field Notes sends the transcript again after speakers are named; replace the OneDrive copy."""
        it = self.state["items"].get(iid)
        tp = it and it.get("transcript_path")
        if not tp:
            return False
        p = Path(tp)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return True

    def _move_to_imported(self, it):
        dest = Path(it["path"]).parent / "Imported"
        try:
            dest.mkdir(exist_ok=True)
            moved = []
            for src in (it["path"], it.get("notes_path")):
                if src and Path(src).exists():
                    target = dest / Path(src).name
                    if target.exists():
                        target = dest / f"{Path(src).stem}_{int(time.time())}{Path(src).suffix}"
                    shutil.move(src, target)
                    moved.append(str(target))
            if moved:
                it["moved_to"] = moved
            it.pop("move_pending", None)
        except Exception as e:
            self.log(f"Could not move {it['name']} to Imported yet: {e}")
            it["move_pending"] = True

    def _pending_moves(self):
        with self.lock:
            changed = False
            for it in self.state["items"].values():
                if it.get("move_pending") and not self.job_active(it.get("job")):
                    self._move_to_imported(it)
                    changed = True
            if changed:
                self._save()

    # ---- scanning
    @staticmethod
    def item_id(p: Path, st) -> str:
        return hashlib.sha1(f"{p}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]

    def scan(self):
        if not self.cfg.get("enabled"):
            return
        self._pending_moves()
        now = time.time()
        for f in self.folders():
            if f["kind"] == "inbox" and not f["exists"]:
                try:
                    Path(f["path"]).mkdir(parents=True, exist_ok=True)
                    self.log(f"Created {f['path']}")
                except Exception:
                    continue
            if f["kind"] == "teams" and not (f.get("on") and f["exists"]):
                continue
            if f["kind"] == "teams" and f["path"] not in self.state["baseline"]:
                self._baseline_teams()
                continue
            folder = Path(f["path"])
            try:
                entries = list(folder.iterdir())
            except Exception:
                continue
            baseline = set(self.state["baseline"].get(f["path"], []))
            for p in entries:
                if not p.is_file() or p.suffix.lower() not in MEDIA or str(p) in baseline:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                key = str(p)
                prev = self._sizes.get(key)
                self._sizes[key] = (st.st_size, st.st_mtime)
                # still syncing/being written: wait until size and time stop changing
                if st.st_size == 0 or prev != (st.st_size, st.st_mtime) or now - st.st_mtime < STABLE_SECONDS:
                    continue
                iid = self.item_id(p, st)
                with self.lock:
                    if any(it["path"] == key for it in self.state["items"].values() if it["status"] not in ("imported", "removed")):
                        continue
                    if iid in self.state["items"]:
                        continue
                    notes = self._notes_for(p)
                    title, attendees = self._title_attendees(p, notes)
                    tdir = self.transcripts_dir()
                    item = {"id": iid, "path": key, "name": p.name, "size": st.st_size, "mtime": st.st_mtime,
                            "kind": f["kind"], "notes_path": str(notes) if notes else None, "status": "new",
                            "found": now, "job": None, "title": title,
                            "transcript_path": str(tdir / f"{p.stem}_transcript.txt") if tdir else None}
                    if self.cfg.get("auto_transcribe"):
                        try:
                            import formats
                            meta = formats.meta_for(p)
                            if notes and not meta:
                                meta = formats.parse_notes_txt(notes.read_text(encoding="utf-8", errors="replace"))
                            out = {"dir": str(tdir), "meta": meta, "overwrite": True, "source": key} if tdir else None
                            item["job"] = self.submit(key, title, attendees, keep_file=True, outputs=out)
                        except Exception as e:
                            self.log(f"Could not queue {p.name}: {e}")
                    self.state["items"][iid] = item
                    self._save()
                    self.log(f"Inbox: found {p.name}" + (" (with notes)" if notes else ""))

    @staticmethod
    def _notes_for(p: Path) -> Path | None:
        for cand in (p.with_name(p.stem + "_notes.txt"), p.with_name(p.stem + ".txt")):
            if cand.exists():
                return cand
        return None

    @staticmethod
    def _title_attendees(p: Path, notes: Path | None):
        title, att = p.stem, []
        meta = read_wav_meta(p) if p.suffix.lower() == ".wav" else None
        if meta and not notes:
            title = (meta.get("title") or "").strip() or title
            att = [a.strip() for a in (meta.get("attendees") or "").splitlines() if a.strip()]
            return title, att
        if notes:
            try:
                lines = notes.read_text(encoding="utf-8", errors="replace").splitlines()
                title = (lines[0] if lines else "").strip() or title
                sec = False
                for l in lines[1:]:
                    l = l.strip()
                    if l.lower().startswith("attendees:"):
                        sec = True
                        continue
                    if sec:
                        if not l:
                            break
                        if l.startswith("- ") and "not recorded" not in l:
                            att.append(l[2:].strip())
            except Exception:
                pass
        return title, att

    # ---- API helpers
    def pending(self) -> list[dict]:
        with self.lock:
            return [dict(it) for it in self.state["items"].values() if it["status"] == "new" and Path(it["path"]).exists()]

    def get(self, iid):
        return self.state["items"].get(iid)

    def mark_imported(self, iid):
        with self.lock:
            it = self.state["items"].get(iid)
            if not it:
                return False
            it["status"] = "imported"
            it["imported"] = time.time()
            if it["kind"] == "inbox":
                # never move a file the transcriber still has to read; the scan loop moves it afterwards
                if self.job_active(it.get("job")):
                    it["move_pending"] = True
                else:
                    self._move_to_imported(it)
            # forget old imported entries after a year (kept so Delete in Field Notes can remove the OneDrive copies)
            cutoff = time.time() - 365 * 86400
            for k in [k for k, v in self.state["items"].items() if v["status"] in ("imported", "removed") and v.get("imported", 0) < cutoff]:
                self.state["items"].pop(k, None)
            self._save()
            return True

    def remove(self, iid):
        """The recording was deleted in Field Notes: delete its OneDrive copies (inbox audio and notes,
        wherever they are now, and the transcript). Teams recordings themselves are never deleted.
        OneDrive keeps deleted files in its recycle bin."""
        with self.lock:
            it = self.state["items"].get(iid)
            if not it:
                return None
            if self.job_active(it.get("job")):
                return {"error": "still transcribing"}
            paths = [it.get("transcript_path")]
            if it["kind"] == "inbox":
                paths += [it.get("path"), it.get("notes_path")] + list(it.get("moved_to") or [])
            removed = []
            for fp in paths:
                try:
                    if fp and Path(fp).is_file():
                        Path(fp).unlink()
                        removed.append(Path(fp).name)
                except Exception as e:
                    self.log(f"Could not delete {fp}: {e}")
            it["status"] = "removed"
            it["imported"] = it.get("imported") or time.time()
            it.pop("move_pending", None)
            self._save()
            self.log(f"Inbox: removed {', '.join(removed) or 'nothing'} for {it['name']}")
            return {"removed": removed}

    def run(self, every: float = 10.0):
        while True:
            try:
                self.scan()
            except Exception as e:
                self.log(f"Inbox scan error: {e}")
            time.sleep(every)
