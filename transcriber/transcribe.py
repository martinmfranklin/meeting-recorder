"""Transcribe audio/video files from File Explorer (right-click > Send to > Transcribe) or the command line.

Writes  <name>_transcript.txt  next to each file (and <name>.srt for videos), with speakers labelled
Speaker 1, 2, ... and, for Field Notes recordings, the title, attendees and notes included.

Uses the running Field Notes Transcriber when it is up (so jobs queue behind each other and the model
is shared); otherwise loads the model itself.

  python transcribe.py FILE [FILE ...] [--srt | --no-srt] [--speakers N] [--no-open] [--pause]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import engine   # noqa: E402
import formats  # noqa: E402

HELPER = "http://127.0.0.1:8787"
MEDIA = {".wav", ".m4a", ".mp3", ".mp4", ".webm", ".ogg", ".mov", ".aac", ".wma", ".flac", ".mkv", ".avi", ".m4v", ".opus"}


def _req(method, path, body=None, token=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(HELPER + path, data=data, method=method,
                               headers={"Content-Type": "application/json", **({"X-FN-Token": token} if token else {})})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read())


def helper_up() -> str | None:
    try:
        h = _req("GET", "/health", timeout=2)
        tok = (engine.app_dir() / "token.txt").read_text(encoding="utf-8").strip()
        return tok if h.get("ok") else None
    except Exception:
        return None


def bar(stage: str, frac: float, name: str):
    n = int(frac * 30)
    sys.stdout.write(f"\r  [{'#' * n}{'.' * (30 - n)}] {int(frac * 100):3d}%  {stage[:34]:<34}")
    sys.stdout.flush()


def via_helper(src: Path, srt: bool, speakers: int, token: str) -> list[str]:
    j = _req("POST", "/local/jobs", {"path": str(src), "srt": srt, "speakers": speakers}, token=token)
    jid = j["id"]
    while True:
        time.sleep(1.5)
        j = _req("GET", f"/jobs/{jid}")
        st = j.get("status")
        stage = f"Waiting ({j['ahead']} ahead)" if j.get("ahead") else (j.get("stage") or st)
        bar(stage, j.get("progress") or 0, src.name)
        if st == "done":
            print()
            try:
                _req("DELETE", f"/jobs/{jid}")
            except Exception:
                pass
            if j.get("output_error"):
                raise RuntimeError(j["output_error"])
            return j.get("outputs") or []
        if st in ("error", "cancelled"):
            print()
            raise RuntimeError(j.get("error") or st)


_local = {}


def in_process(src: Path, srt: bool, speakers: int, model: str) -> list[str]:
    if "asr" not in _local:
        print("  Loading speech model (first time downloads about 1.6 GB)...")
        _local["dia"] = engine.Diarizer()
        _local["asr"] = engine.SherpaWhisperTestASR(_local["test"]) if _local.get("test") else engine.FasterWhisperASR(model)
        print(f"  Using {_local['asr'].device}")
    meta = formats.meta_for(src)
    att = [a for a in (meta or {}).get("attendees", "").splitlines() if a.strip()]
    res = engine.run_job(engine.Job(path=str(src), title=(meta or {}).get("title") or src.stem, attendees=att,
                                    num_speakers=speakers),
                         _local["asr"], _local["dia"], lambda s, f: bar(s, f, src.name))
    print()
    return formats.write_outputs(res, src, src.parent, meta, srt)


def main():
    ap = argparse.ArgumentParser(description="Transcribe audio/video files with speaker labels, on this computer.")
    ap.add_argument("files", nargs="*")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--srt", dest="srt", action="store_true", default=None, help="also write subtitles (default for video)")
    g.add_argument("--no-srt", dest="srt", action="store_false")
    ap.add_argument("--speakers", type=int, default=0, help="number of speakers if known (0 = detect)")
    ap.add_argument("--model", default="large-v3-turbo", help="used only when the background transcriber is not running")
    ap.add_argument("--no-open", action="store_true", help="do not open the transcript when done")
    ap.add_argument("--pause", action="store_true", help="wait for Enter before closing (used by Send to)")
    ap.add_argument("--test-asr", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.test_asr:
        _local["test"] = a.test_asr

    files = [Path(f) for f in a.files]
    todo = [f for f in files if f.is_file() and f.suffix.lower() in MEDIA]
    skipped = [f for f in files if f not in todo]
    print("Field Notes Transcriber\n")
    for f in skipped:
        print(f"Skipping {f.name}: not an audio or video file")
    if not todo:
        print("Nothing to transcribe. Select audio or video files, then right-click > Send to > Transcribe.")
    token = helper_up() if todo else None
    if todo:
        print("Using the background transcriber.\n" if token else "Background transcriber not running; transcribing here.\n")
    written, failed = [], []
    for i, f in enumerate(todo, 1):
        srt = a.srt if a.srt is not None else f.suffix.lower() in formats.VIDEO
        print(f"{i}/{len(todo)}  {f.name}")
        t0 = time.time()
        try:
            out = via_helper(f, srt, a.speakers, token) if token else in_process(f, srt, a.speakers, a.model)
            written += out
            for o in out:
                print(f"  Saved {o}")
            print(f"  Done in {int(time.time() - t0)} s\n")
        except Exception as e:
            print(f"\n  Failed: {e}\n")
            failed.append(f.name)
    if written and not a.no_open and os.name == "nt":
        try:
            os.startfile(next(w for w in written if w.endswith(".txt")))  # type: ignore[attr-defined]
        except Exception:
            pass
    if failed:
        print("Failed: " + ", ".join(failed))
    if a.pause:
        input("Press Enter to close.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
