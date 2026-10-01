"""Field Notes Transcriber: local helper for the Field Notes web app.

Listens on http://127.0.0.1:8787 (this computer only). Field Notes sends a recording, this
transcribes it with speaker labels and returns the result. Audio never leaves the computer.

  GET    /health          status, model, CPU/GPU
  POST   /jobs            body = audio/video file; headers X-FN-Title, X-FN-Attendees (URI-encoded JSON list),
                          X-FN-Speakers (optional int), X-FN-Filename
  GET    /jobs/<id>       progress, then the transcript
  DELETE /jobs/<id>       cancel or remove
  GET    /                small status page

Run:  python server.py [--model large-v3-turbo] [--device auto|cpu|cuda] [--port 8787]
"""
from __future__ import annotations

import argparse
import json
import queue
import re
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import engine
import inbox as inbox_mod
import mimetypes
import os

VERSION = "1.2.0"
DEFAULT_ORIGINS = [r"https://martinmfranklin\.github\.io", r"http://localhost(:\d+)?", r"http://127\.0\.0\.1(:\d+)?"]


class Cancelled(Exception):
    pass


class State:
    def __init__(self, args):
        self.args = args
        self.status = "starting"      # starting | loading | ready | error
        self.detail = ""
        self.asr = None
        self.diarizer = None
        self.jobs: dict[str, dict] = {}
        self.queue: "queue.Queue[str]" = queue.Queue()
        self.lock = threading.Lock()
        self.jobs_dir = engine.app_dir() / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._load_saved_jobs()
        self.inbox = None if args.no_inbox else inbox_mod.Inbox(self.submit_path, log=lambda m: print(m, flush=True))

    def submit_path(self, path, title, attendees, keep_file=False, num_speakers=0, filename=None):
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "status": "queued", "stage": "Queued", "progress": 0.0, "created": time.time(),
               "title": title or "", "attendees": attendees or [], "num_speakers": num_speakers,
               "filename": filename or Path(path).name, "bytes": Path(path).stat().st_size, "_path": str(path), "_keep": keep_file}
        self.jobs[jid] = job
        self.queue.put(jid)
        return jid

    # -- persistence: finished jobs survive a restart so the app can still collect them
    def _load_saved_jobs(self):
        cutoff = time.time() - 7 * 86400
        for f in self.jobs_dir.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    continue
                j = json.loads(f.read_text(encoding="utf-8"))
                if j.get("status") in ("queued", "running"):
                    j.update(status="error", error="The transcriber was restarted before this finished. Please transcribe again.")
                self.jobs[j["id"]] = j
            except Exception:
                pass

    def save(self, job):
        p = self.jobs_dir / f"{job['id']}.json"
        p.write_text(json.dumps({k: v for k, v in job.items() if not k.startswith("_")}), encoding="utf-8")

    def log(self, msg):
        self.detail = msg
        print(msg, flush=True)

    def load_models(self):
        try:
            self.status = "loading"
            self.log("Loading speaker model")
            self.diarizer = engine.Diarizer(log=self.log)
            self.log(f"Loading speech model {self.args.model} (first run downloads about 1.6 GB)")
            if self.args.test_asr:
                self.asr = engine.SherpaWhisperTestASR(self.args.test_asr)
            else:
                self.asr = engine.FasterWhisperASR(self.args.model, self.args.device, log=self.log)
            self.status = "ready"
            self.log(f"Ready on {self.asr.device}")
        except Exception as e:
            self.status = "error"
            self.log(f"Could not load models: {e}")
            traceback.print_exc()

    def worker(self):
        while True:
            jid = self.queue.get()
            job = self.jobs.get(jid)
            if not job or job.get("status") != "queued":
                continue
            while self.status in ("starting", "loading"):
                job.update(stage="Waiting for models to load", progress=0.0)
                time.sleep(1)
            if self.status != "ready":
                job.update(status="error", error=f"Models are not available: {self.detail}")
                self.save(job)
                continue
            job.update(status="running", started=time.time())

            def progress(stage, frac):
                if job.get("_cancel"):
                    raise Cancelled()
                job["stage"], job["progress"] = stage, round(float(frac), 4)

            try:
                res = engine.run_job(engine.Job(path=job["_path"], title=job.get("title", ""),
                                                attendees=job.get("attendees") or [],
                                                num_speakers=job.get("num_speakers") or 0),
                                     self.asr, self.diarizer, progress)
                job.update(status="done", progress=1.0, stage="Done", result=res, finished=time.time())
            except Cancelled:
                job.update(status="cancelled", stage="Cancelled")
            except Exception as e:
                traceback.print_exc()
                job.update(status="error", error=str(e) or e.__class__.__name__)
            finally:
                if not job.get("_keep"):
                    try:
                        Path(job["_path"]).unlink(missing_ok=True)
                    except Exception:
                        pass
                self.save(job)


def make_handler(st: State):
    allowed = [re.compile(p + "$") for p in (st.args.allow_origin or DEFAULT_ORIGINS)]

    class H(BaseHTTPRequestHandler):
        server_version = f"FieldNotesTranscriber/{VERSION}"

        def log_message(self, fmt, *a):
            pass

        def _origin_ok(self):
            o = self.headers.get("Origin")
            return o if o and any(r.match(o) for r in allowed) else None

        def _cors(self):
            o = self._origin_ok()
            if o:
                self.send_header("Access-Control-Allow-Origin", o)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-FN-Title, X-FN-Attendees, X-FN-Speakers, X-FN-Filename, X-FN-Client")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Access-Control-Max-Age", "600")

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _reject_foreign(self):
            # A browser page from any other site must not be able to use this helper.
            if self.headers.get("Origin") and not self._origin_ok():
                self._json(403, {"error": "origin not allowed"})
                return True
            return False

        def do_OPTIONS(self):
            self.send_response(204 if self._origin_ok() else 403)
            self._cors()
            self.end_headers()

        def do_GET(self):
            if self._reject_foreign():
                return
            path = urllib.parse.urlparse(self.path).path
            if path == "/health":
                busy = sum(1 for j in st.jobs.values() if j.get("status") in ("queued", "running"))
                return self._json(200, {"ok": True, "version": VERSION, "status": st.status, "detail": st.detail,
                                        "model": getattr(st.asr, "model_name", st.args.model),
                                        "device": getattr(st.asr, "device", ""), "busy": busy})
            m = re.fullmatch(r"/jobs/([\w-]+)", path)
            if m:
                j = st.jobs.get(m.group(1))
                if not j:
                    return self._json(404, {"error": "unknown job"})
                out = {k: v for k, v in j.items() if not k.startswith("_")}
                if j.get("status") == "queued":
                    ahead = [x for x in st.jobs.values() if x.get("status") in ("queued", "running") and x["created"] < j["created"]]
                    out["ahead"] = len(ahead)
                return self._json(200, out)
            if path == "/inbox":
                if not st.inbox:
                    return self._json(200, {"enabled": False, "folders": [], "items": []})
                items = []
                for it in st.inbox.pending():
                    j = st.jobs.get(it.get("job") or "")
                    items.append({k: it[k] for k in ("id", "name", "size", "mtime", "kind", "title", "job")} |
                                 {"has_notes": bool(it.get("notes_path")), "job_status": j["status"] if j else None})
                return self._json(200, {"enabled": st.inbox.cfg.get("enabled"), "auto_transcribe": st.inbox.cfg.get("auto_transcribe"),
                                        "watch_teams": st.inbox.cfg.get("watch_teams"), "folders": st.inbox.folders(), "items": items})
            m = re.fullmatch(r"/inbox/(\w+)/(file|notes)", path)
            if m and st.inbox:
                it = st.inbox.get(m.group(1))
                fp = it and (it["path"] if m.group(2) == "file" else it.get("notes_path"))
                if not fp or not Path(fp).exists():
                    return self._json(404, {"error": "not found"})
                return self._file(fp)
            if path == "/":
                return self._page()
            self._json(404, {"error": "not found"})

        def _file(self, fp):
            size = os.path.getsize(fp)
            ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(fp, "rb") as f:
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    self.wfile.write(b)

        def do_POST(self):
            if self._reject_foreign():
                return
            path = urllib.parse.urlparse(self.path).path
            if path.startswith("/inbox") and st.inbox:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                if path == "/inbox/config":
                    try:
                        st.inbox.set_config(**json.loads(body or b"{}"))
                    except Exception as e:
                        return self._json(400, {"error": str(e)})
                    return self._json(200, {"ok": True, "folders": st.inbox.folders(), "watch_teams": st.inbox.cfg.get("watch_teams")})
                m = re.fullmatch(r"/inbox/(\w+)/imported", path)
                if m:
                    return self._json(200 if st.inbox.mark_imported(m.group(1)) else 404, {"ok": True})
                return self._json(404, {"error": "not found"})
            if path != "/jobs":
                return self._json(404, {"error": "not found"})
            if self.headers.get("Origin") and self.headers.get("X-FN-Client") != "field-notes":
                return self._json(403, {"error": "missing client header"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return self._json(400, {"error": "empty upload"})
            dec = lambda h: urllib.parse.unquote(self.headers.get(h) or "")
            name = re.sub(r"[^\w.\- ]", "_", dec("X-FN-Filename") or "audio")[-80:]
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix="_" + name, dir=engine.app_dir())
            left = n
            with tmp:
                while left > 0:
                    b = self.rfile.read(min(1 << 20, left))
                    if not b:
                        break
                    tmp.write(b)
                    left -= len(b)
            if left:
                Path(tmp.name).unlink(missing_ok=True)
                return self._json(400, {"error": "upload interrupted"})
            try:
                attendees = json.loads(dec("X-FN-Attendees") or "[]")
            except Exception:
                attendees = []
            try:
                ns = int(self.headers.get("X-FN-Speakers") or 0)
            except ValueError:
                ns = 0
            jid = uuid.uuid4().hex[:12]
            job = {"id": jid, "status": "queued", "stage": "Queued", "progress": 0.0, "created": time.time(),
                   "title": dec("X-FN-Title"), "attendees": attendees if isinstance(attendees, list) else [],
                   "num_speakers": max(0, min(ns, 20)), "filename": name, "bytes": n, "_path": tmp.name}
            st.jobs[jid] = job
            st.queue.put(jid)
            self._json(202, {"id": jid, "status": "queued"})

        def do_DELETE(self):
            if self._reject_foreign():
                return
            m = re.fullmatch(r"/jobs/([\w-]+)", urllib.parse.urlparse(self.path).path)
            j = st.jobs.get(m.group(1)) if m else None
            if not j:
                return self._json(404, {"error": "unknown job"})
            if j["status"] in ("queued", "running"):
                j["_cancel"] = True
                if j["status"] == "queued":
                    j.update(status="cancelled", stage="Cancelled")
                    st.save(j)
            else:
                st.jobs.pop(j["id"], None)
                (st.jobs_dir / f"{j['id']}.json").unlink(missing_ok=True)
            self._json(200, {"ok": True})

        def _page(self):
            rows = "".join(
                f"<tr><td>{time.strftime('%b %d %H:%M', time.localtime(j['created']))}</td><td>{j.get('title') or j.get('filename')}</td>"
                f"<td>{j['status']}</td><td>{int(100 * j.get('progress', 0))}%</td></tr>"
                for j in sorted(st.jobs.values(), key=lambda x: -x["created"])[:20])
            html = f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=5><title>Field Notes Transcriber</title>
<style>body{{font:15px system-ui,sans-serif;max-width:640px;margin:32px auto;padding:0 16px;color:#18212b}}td,th{{padding:4px 10px;text-align:left;border-bottom:1px solid #ddd}}</style>
<h1>Field Notes Transcriber</h1><p>Status: <b>{st.status}</b> &middot; {st.detail}</p>
<p>Model {getattr(st.asr, 'model_name', st.args.model)} on {getattr(st.asr, 'device', '...')} &middot; version {VERSION}</p>
<p>Start transcriptions from Field Notes (Details &rarr; Transcribe). Audio stays on this computer.</p>
<p>Watching: {'; '.join(f"{f['path']}{'' if f['kind']=='inbox' or f.get('on') else ' (off)'}" for f in (st.inbox.folders() if st.inbox else [])) or 'nothing'}</p>
<table><tr><th>When</th><th>Recording</th><th>Status</th><th></th></tr>{rows}</table>"""
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return H


def _log_to_file_if_windowless():
    """pythonw (no console) has no stdout; keep a log file instead."""
    import sys
    if sys.stdout is None or sys.stderr is None:
        f = open(engine.app_dir() / "transcriber.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = f


def main():
    _log_to_file_if_windowless()
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--allow-origin", action="append", help="regex of an extra allowed web origin")
    ap.add_argument("--prefetch", action="store_true", help="download and load models, then exit")
    ap.add_argument("--no-inbox", action="store_true", help="do not watch the OneDrive inbox")
    ap.add_argument("--test-asr", help=argparse.SUPPRESS)  # sherpa whisper model dir, for testing only
    args = ap.parse_args()
    st = State(args)
    if args.prefetch:
        st.load_models()
        raise SystemExit(0 if st.status == "ready" else 1)
    threading.Thread(target=st.load_models, daemon=True).start()
    threading.Thread(target=st.worker, daemon=True).start()
    if st.inbox:
        threading.Thread(target=st.inbox.run, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(st))
    except OSError:
        print(f"Port {args.port} is in use; the transcriber is probably already running.", flush=True)
        raise SystemExit(0)
    print(f"Field Notes Transcriber {VERSION} on http://127.0.0.1:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
