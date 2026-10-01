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


def find_onedrive() -> Path | None:
    for var in ("OneDriveCommercial", "OneDrive", "OneDriveConsumer"):
        p = os.environ.get(var)
        if p and Path(p).is_dir():
            return Path(p)
    p = Path.home() / "OneDrive"
    return p if p.is_dir() else None


class Inbox:
    def __init__(self, submit, log=print):
        """submit(path, title, attendees, keep_file=True) -> job id"""
        self.submit, self.log = submit, log
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

    # ---- scanning
    @staticmethod
    def item_id(p: Path, st) -> str:
        return hashlib.sha1(f"{p}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]

    def scan(self):
        if not self.cfg.get("enabled"):
            return
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
                    if any(it["path"] == key for it in self.state["items"].values() if it["status"] != "imported"):
                        continue
                    if iid in self.state["items"]:
                        continue
                    notes = self._notes_for(p)
                    title, attendees = self._title_attendees(p, notes)
                    item = {"id": iid, "path": key, "name": p.name, "size": st.st_size, "mtime": st.st_mtime,
                            "kind": f["kind"], "notes_path": str(notes) if notes else None, "status": "new",
                            "found": now, "job": None, "title": title}
                    if self.cfg.get("auto_transcribe"):
                        try:
                            item["job"] = self.submit(key, title, attendees, keep_file=True)
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
                dest = Path(it["path"]).parent / "Imported"
                try:
                    dest.mkdir(exist_ok=True)
                    for src in (it["path"], it.get("notes_path")):
                        if src and Path(src).exists():
                            target = dest / Path(src).name
                            if target.exists():
                                target = dest / f"{Path(src).stem}_{int(time.time())}{Path(src).suffix}"
                            shutil.move(src, target)
                except Exception as e:
                    self.log(f"Could not move {it['name']} to Imported: {e}")
            # forget old imported entries after 30 days
            cutoff = time.time() - 30 * 86400
            for k in [k for k, v in self.state["items"].items() if v["status"] == "imported" and v.get("imported", 0) < cutoff]:
                self.state["items"].pop(k, None)
            self._save()
            return True

    def run(self, every: float = 10.0):
        while True:
            try:
                self.scan()
            except Exception as e:
                self.log(f"Inbox scan error: {e}")
            time.sleep(every)
