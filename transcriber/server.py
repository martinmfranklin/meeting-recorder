"""Field Notes Transcriber: local helper for the Field Notes web app.

Listens on http://127.0.0.1:8787 (this computer only). Field Notes sends a recording, this
transcribes it with speaker labels and returns the result. Audio never leaves the computer.

  GET    /health          status, model, CPU/GPU
  POST   /jobs            body = audio/video file; headers X-FN-Title, X-FN-Attendees (URI-encoded JSON list),
                          X-FN-Speakers (optional int), X-FN-Filename,
                          X-FN-Reuse (1 = reuse this recording's cached words and speaker analysis; only regroup speakers),
                          X-FN-MinSpeakers (sent by Field Notes 2.6.0; ignored)
  GET    /jobs/<id>       progress, then the transcript
  DELETE /jobs/<id>       cancel or remove
  POST   /local/jobs      {"path": ..., "srt": bool} from programs on this computer (needs X-FN-Token, no browser Origin)
  GET    /                small status page

Models load when there is work and unload after --idle-minutes (default 10) so the helper stays small when idle.

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
import formats
import secrets
import inbox as inbox_mod
import html as html_mod
import mimetypes
import os

VERSION = "1.6.0"
DEFAULT_ORIGINS = [r"https://martinmfranklin\.github\.io", r"http://localhost(:\d+)?", r"http://127\.0\.0\.1(:\d+)?"]


class Cancelled(Exception):
    pass


class State:
    def __init__(self, args):
        self.args = args
        self.status = "idle"          # idle (models not in memory) | loading | ready | error
        self.last_device = ""
        self.last_used = time.time()
        self.detail = ""
        self.asr = None
        self.diarizer = None
        self.jobs: dict[str, dict] = {}
        self.queue: "queue.Queue[str]" = queue.Queue()
        self.lock = threading.Lock()
        self.jobs_dir = engine.app_dir() / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._load_saved_jobs()
        self.inbox = None if args.no_inbox else inbox_mod.Inbox(self.submit_path, log=lambda m: print(m, flush=True),
                                                                   job_active=lambda jid: bool(jid) and self.jobs.get(jid, {}).get("status") in ("queued", "running"))
        tok = engine.app_dir() / "token.txt"
        if not tok.exists():
            tok.write_text(secrets.token_hex(16), encoding="utf-8")
        self.token = tok.read_text(encoding="utf-8").strip()

    def submit_path(self, path, title, attendees, keep_file=False, num_speakers=0, filename=None, outputs=None, reuse=False,
                    min_speakers=None):
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "status": "queued", "stage": "Queued", "progress": 0.0, "created": time.time(),
               "title": title or "", "attendees": attendees or [], "num_speakers": num_speakers, "reuse": bool(reuse),
               "min_speakers": min(20, len(attendees or [])) if min_speakers is None else min_speakers,
               "filename": filename or Path(path).name, "bytes": Path(path).stat().st_size, "_path": str(path), "_keep": keep_file}
        if outputs:
            job["_outputs"] = outputs
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

    # ---- model process: speech models live in a child process that is stopped when idle,
    # so the helper drops back to a few tens of MB between jobs.
    def _start_engine(self) -> bool:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        self.conn, child_end = ctx.Pipe()
        self.child = ctx.Process(target=engine_process, args=(child_end, self.args.model, self.args.device, self.args.test_asr),
                                 daemon=True)
        self.child.start()
        self.status = "loading"
        while True:
            if self.conn.poll(1):
                m = self.conn.recv()
                if m[0] == "log":
                    self.log(m[1])
                elif m[0] == "ready":
                    self.status, self.last_device = "ready", m[1]
                    self.log(f"Ready on {m[1]}")
                    return True
                elif m[0] == "fatal":
                    self.status = "error"
                    self.log(f"Could not load models: {m[1]}")
                    return False
            elif not self.child.is_alive():
                self.status = "error"
                self.log("The model process stopped while loading")
                return False

    def _engine_alive(self) -> bool:
        return getattr(self, "child", None) is not None and self.child.is_alive()

    def _stop_engine(self, why="Idle"):
        if self._engine_alive():
            try:
                self.conn.send(("stop",))
            except Exception:
                pass
            self.child.join(10)
            if self.child.is_alive():
                self.child.terminate()
            try:
                self.conn.close()
            except Exception:
                pass
        self.child = None
        self.status = "idle"
        self.log(f"{why}: speech model unloaded, memory freed")

    def idle_watch(self):
        limit = self.args.idle_minutes * 60
        while limit > 0:
            time.sleep(15)
            busy = any(j.get("status") in ("queued", "running") for j in self.jobs.values())
            if self.status == "ready" and not busy and time.time() - self.last_used > limit:
                with self.lock:
                    if not any(j.get("status") in ("queued", "running") for j in self.jobs.values()):
                        self._stop_engine()

    def load_models(self):
        """In-process load, used by --prefetch to download and verify the models."""
        try:
            self.status = "loading"
            self.log("Loading speaker model")
            self.diarizer = engine.Diarizer(log=self.log)
            self.log(f"Loading speech model {self.args.model} (first run downloads about 1.6 GB)")
            self.asr = (engine.SherpaWhisperTestASR(self.args.test_asr) if self.args.test_asr
                        else engine.FasterWhisperASR(self.args.model, self.args.device, log=self.log))
            self.status, self.last_device = "ready", self.asr.device
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
            with self.lock:
                if not self._engine_alive():
                    job.update(stage="Loading speech model", progress=0.0)
                    if not self._start_engine():
                        job.update(status="error", error=f"Models are not available: {self.detail}")
                        self.save(job)
                        continue
            job.update(status="running", started=time.time())
            result = None
            try:
                self.conn.send(("job", jid, job["_path"], job.get("title", ""), job.get("attendees") or [], job.get("num_speakers") or 0,
                                bool(job.get("reuse")), int(job.get("min_speakers") or 0)))
                while True:
                    if job.get("_cancel") and not job.get("_cancel_sent"):
                        self.conn.send(("cancel", jid))
                        job["_cancel_sent"] = time.time()
                    if job.get("_cancel_sent") and time.time() - job["_cancel_sent"] > 5:
                        # the model is busy inside a long step; stop the process (it restarts for the next job)
                        self.child.terminate()
                        self.child.join(5)
                        self.child = None
                        try:
                            self.conn.close()
                        except Exception:
                            pass
                        self.status = "idle"
                        job.update(status="cancelled", stage="Cancelled")
                        self.log("Cancelled; model process stopped")
                        break
                    if self.conn.poll(0.5):
                        m = self.conn.recv()
                        if m[0] == "progress":
                            job["stage"], job["progress"] = m[2], round(m[3], 4)
                        elif m[0] == "log":
                            self.log(m[1])
                        elif m[0] == "done":
                            result = m[2]
                            break
                        elif m[0] == "cancelled":
                            job.update(status="cancelled", stage="Cancelled")
                            break
                        elif m[0] == "error":
                            job.update(status="error", error=m[2])
                            break
                    elif not self._engine_alive():
                        job.update(status="error", error="The model process stopped unexpectedly (out of memory?).")
                        self.status = "idle"
                        break
                if result is not None:
                    job.update(status="done", progress=1.0, stage="Done", result=result, finished=time.time())
                    out = job.get("_outputs")
                    if out:
                        try:
                            job["outputs"] = formats.write_outputs(result, Path(out.get("source") or job["_path"]), Path(out["dir"]),
                                                                   out.get("meta"), bool(out.get("srt")), bool(out.get("overwrite")))
                            print("Wrote " + ", ".join(job["outputs"]), flush=True)
                        except Exception as e:
                            job["output_error"] = str(e)
                            print(f"Could not write transcript files: {e}", flush=True)
            except Exception as e:
                traceback.print_exc()
                job.update(status="error", error=str(e) or e.__class__.__name__)
            finally:
                self.last_used = time.time()
                if not job.get("_keep"):
                    try:
                        Path(job["_path"]).unlink(missing_ok=True)
                    except Exception:
                        pass
                self.save(job)


class _ChildCancel(Exception):
    pass


def engine_process(conn, model, device, test_asr):
    """Runs in its own process: loads the models once, transcribes jobs, exits on 'stop'."""
    try:
        _engine_loop(conn, model, device, test_asr)
    except (EOFError, BrokenPipeError, ConnectionResetError, OSError, _ChildCancel):
        pass  # the helper closed the connection (cancel, idle stop or shutdown): exit quietly


def _engine_loop(conn, model, device, test_asr):
    try:
        log = lambda m: conn.send(("log", m))
        dia = engine.Diarizer(log=log)
        log(f"Loading speech model {model}")
        asr = engine.SherpaWhisperTestASR(test_asr) if test_asr else engine.FasterWhisperASR(model, device, log=log)
        conn.send(("ready", asr.device))
    except Exception as e:
        conn.send(("fatal", str(e)))
        return
    while True:
        msg = conn.recv()
        if msg[0] == "stop":
            return
        if msg[0] != "job":
            continue
        _, jid, path, title, att, ns, reuse, mins = msg
        state = {"cancel": False}

        def progress(stage, frac):
            while conn.poll():
                m = conn.recv()
                if m[0] == "cancel" and m[1] == jid:
                    state["cancel"] = True
            if state["cancel"]:
                raise _ChildCancel()
            conn.send(("progress", jid, stage, float(frac)))

        try:
            res = engine.run_job(engine.Job(path=path, title=title, attendees=att, num_speakers=ns, reuse=reuse, min_speakers=mins), asr, dia, progress)
            conn.send(("done", jid, res))
        except _ChildCancel:
            conn.send(("cancelled", jid))
        except Exception as e:
            conn.send(("error", jid, str(e) or e.__class__.__name__))


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
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-FN-Title, X-FN-Attendees, X-FN-Speakers, X-FN-Filename, X-FN-Client, X-FN-Reuse, X-FN-MinSpeakers")
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
                                        "model": getattr(st.asr, "model_name", None) or st.args.model,
                                        "device": getattr(st.asr, "device", None) or st.last_device, "busy": busy,
                                        "idle_minutes": st.args.idle_minutes})
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
                    items.append({k: it.get(k) for k in ("id", "name", "size", "mtime", "kind", "title", "job", "transcript_path")} |
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
                m = re.fullmatch(r"/inbox/(\w+)/transcript", path)
                if m:
                    ok = st.inbox.write_transcript(m.group(1), body.decode("utf-8", errors="replace"))
                    return self._json(200 if ok else 404, {"ok": ok})
                m = re.fullmatch(r"/inbox/(\w+)/remove", path)
                if m:
                    res = st.inbox.remove(m.group(1))
                    if res is None:
                        return self._json(404, {"error": "unknown item"})
                    return self._json(409 if "error" in res else 200, res)
                m = re.fullmatch(r"/inbox/(\w+)/imported", path)
                if m:
                    return self._json(200 if st.inbox.mark_imported(m.group(1)) else 404, {"ok": True})
                return self._json(404, {"error": "not found"})
            if path == "/local/jobs":
                if self.headers.get("Origin") or not secrets.compare_digest(self.headers.get("X-FN-Token") or "", st.token):
                    return self._json(403, {"error": "local programs only"})
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    req = json.loads(self.rfile.read(n) or b"{}")
                    src = Path(req["path"])
                    assert src.is_file()
                except Exception:
                    return self._json(400, {"error": "path must be an existing file"})
                meta = formats.meta_for(src)
                att = [a for a in (meta or {}).get("attendees", "").splitlines() if a.strip()]
                jid = st.submit_path(str(src), (meta or {}).get("title") or src.stem, att, keep_file=True,
                                     num_speakers=int(req.get("speakers") or 0), reuse=bool(req.get("reuse")),
                                     outputs={"dir": req.get("out_dir") or str(src.parent), "srt": bool(req.get("srt")), "meta": meta})
                return self._json(202, {"id": jid, "status": "queued"})
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
            try:
                mins = int(self.headers.get("X-FN-MinSpeakers") or 0)
            except ValueError:
                mins = 0
            jid = uuid.uuid4().hex[:12]
            job = {"id": jid, "status": "queued", "stage": "Queued", "progress": 0.0, "created": time.time(),
                   "title": dec("X-FN-Title"), "attendees": attendees if isinstance(attendees, list) else [],
                   "num_speakers": max(0, min(ns, 20)), "reuse": self.headers.get("X-FN-Reuse") == "1",
                   "min_speakers": max(0, min(mins, 20)), "filename": name, "bytes": n, "_path": tmp.name}
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
            def rm_btn(j):
                if j["status"] in ("queued", "running"):
                    return ""
                return "<button onclick='rm(" + html_mod.escape(json.dumps([j['id']]), quote=True) + ")'>Remove</button>"
            rows = "".join(
                f"<tr><td>{time.strftime('%b %d %H:%M', time.localtime(j['created']))}</td><td>{j.get('title') or j.get('filename')}</td>"
                f"<td>{j['status']}</td><td>{int(100 * j.get('progress', 0))}%</td>"
                f"<td>{rm_btn(j)}</td></tr>"
                for j in sorted(st.jobs.values(), key=lambda x: -x["created"])[:20])
            done = [j["id"] for j in st.jobs.values() if j["status"] not in ("queued", "running")]
            clear = ("<p><button onclick='rm(" + html_mod.escape(json.dumps(done), quote=True) + ")'>Clear finished and failed</button> "
                     "<small>Removes them from this list only. Transcripts already saved (in Field Notes, OneDrive or next to your files) are kept.</small></p>") if done else ""
            html = f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=5><title>Field Notes Transcriber</title>
<style>body{{font:15px system-ui,sans-serif;max-width:640px;margin:32px auto;padding:0 16px;color:#18212b}}td,th{{padding:4px 10px;text-align:left;border-bottom:1px solid #ddd}}</style>
<h1>Field Notes Transcriber</h1><p>Status: <b>{st.status}</b> &middot; {st.detail}</p>
<p>Model {st.args.model}{' on ' + st.last_device if st.last_device else ''} &middot; {'loaded' if st.status == 'ready' else 'loads when needed, unloads after ' + str(st.args.idle_minutes) + ' idle minutes'} &middot; version {VERSION}</p>
<p>Start transcriptions from Field Notes (Details &rarr; Transcribe). Audio stays on this computer.</p>
<p>Watching: {'; '.join(f"{f['path']}{'' if f['kind']=='inbox' or f.get('on') else ' (off)'}" for f in (st.inbox.folders() if st.inbox else [])) or 'nothing'}</p>
<table><tr><th>When</th><th>Recording</th><th>Status</th><th></th><th></th></tr>{rows}</table>
{clear}
<script>async function rm(ids){{for(const id of ids)await fetch('/jobs/'+id,{{method:'DELETE'}});location.reload()}}</script>"""
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
    ap.add_argument("--idle-minutes", type=float, default=10, help="unload models after this many idle minutes (0 = keep loaded)")
    ap.add_argument("--test-asr", help=argparse.SUPPRESS)  # sherpa whisper model dir, for testing only
    args = ap.parse_args()
    st = State(args)
    if args.prefetch:
        st.load_models()
        raise SystemExit(0 if st.status == "ready" else 1)
    if args.idle_minutes <= 0:
        threading.Thread(target=st._start_engine, daemon=True).start()
    else:
        threading.Thread(target=st.idle_watch, daemon=True).start()
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
