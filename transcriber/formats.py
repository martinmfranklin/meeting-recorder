"""Write transcripts as text (same layout as Field Notes) and SRT subtitles."""
from __future__ import annotations

import time
from pathlib import Path

VIDEO = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".m4v"}


def hms(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def _name(sid: str, names: dict | None) -> str:
    n = (names or {}).get(sid, "").strip()
    return n or f"Speaker {sid.lstrip('S')}"


def _lines(result: dict, names: dict | None, notes: list):
    out = []
    for u in result.get("utterances", []):
        nm = _name(u["speaker"], names)
        if out and out[-1][0] == "u" and out[-1][1] == nm and u["start"] - out[-1][4] < 3:
            out[-1] = ("u", nm, out[-1][2], out[-1][3] + " " + u["text"], u["end"])
        else:
            out.append(("u", nm, u["start"], u["text"], u["end"]))
    timed = [("n", None, k["t"] / 1000.0, k["note"], None) for k in notes if isinstance(k.get("t"), (int, float))]
    merged = sorted(out + timed, key=lambda x: (x[2], 0 if x[0] == "u" else 1))
    after = [("n", None, None, k["note"], None) for k in notes if not isinstance(k.get("t"), (int, float))]
    return merged + after


def to_text(result: dict, source_name: str, meta: dict | None = None, names: dict | None = None) -> str:
    meta = meta or {}
    title = (meta.get("title") or Path(source_name).stem).strip()
    when = meta.get("createdAt")
    when_s = time.strftime("%b %d, %Y %H:%M", time.localtime(when / 1000)) if when else ""
    att = [a.strip() for a in (meta.get("attendees") or "").splitlines() if a.strip()]
    notes = [k for k in (meta.get("markers") or []) if (k.get("note") or "").strip()]
    L = [title]
    if when_s:
        L.append(when_s)
    L.append(f"Length: {hms(result.get('duration', 0))}")
    L.append(f"Source: {source_name}")
    if att:
        L += ["", "Attendees:"] + [f"- {a}" for a in att]
    L += ["", "Speakers:"]
    for s in result.get("speakers", []):
        L.append(f"- {_name(s['id'], names)} ({hms(s['talk_sec'])} talking)")
    L += ["", f"Transcribed on this computer with Whisper {result.get('model', '')} ({result.get('device', '')})."
              + (" Lines starting \"Note\" are notes taken during the meeting." if notes else ""), ""]
    if not result.get("utterances"):
        L.append("No speech was found in this recording.")
    for kind, nm, start, text, _ in _lines(result, names, notes):
        if kind == "u":
            L += [f"[{hms(start)}] {nm}: {text}", ""]
        elif start is None:
            L += [f"Note (after meeting): {text}", ""]
        else:
            L += [f"    Note [{hms(start)}]: {text}", ""]
    return "\n".join(L).rstrip() + "\n"


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def to_srt(result: dict, names: dict | None = None, max_len: float = 7.0) -> str:
    """Subtitles: one cue per utterance, long utterances split into ~7 s pieces by words."""
    cues = []
    for u in result.get("utterances", []):
        words, dur = u["text"].split(), max(0.01, u["end"] - u["start"])
        parts = max(1, int(dur // max_len) + (1 if dur % max_len > 1 else 0))
        per = max(1, -(-len(words) // parts))
        for i in range(0, len(words), per):
            a = u["start"] + dur * i / len(words)
            b = u["start"] + dur * min(len(words), i + per) / len(words)
            cues.append((a, b, f"{_name(u['speaker'], names)}: " + " ".join(words[i:i + per])))
    return "\n".join(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{t}\n" for i, (a, b, t) in enumerate(cues, 1))


def free_path(p: Path) -> Path:
    """Do not overwrite an existing file: name (2).txt, name (3).txt..."""
    if not p.exists():
        return p
    for i in range(2, 1000):
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not q.exists():
            return q
    return p


def write_outputs(result: dict, source: Path, out_dir: Path, meta: dict | None = None, srt: bool = False,
                  overwrite: bool = False) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    txt = out_dir / f"{source.stem}_transcript.txt"
    txt = txt if overwrite else free_path(txt)
    txt.write_text(to_text(result, source.name, meta), encoding="utf-8")
    written = [str(txt)]
    if srt:
        sp = out_dir / f"{source.stem}.srt"
        sp = sp if overwrite else free_path(sp)
        sp.write_text(to_srt(result), encoding="utf-8")
        written.append(str(sp))
    return written


def parse_notes_txt(text: str) -> dict:
    """Read a Field Notes _notes.txt back into the same shape as embedded WAV metadata."""
    import re as _re
    L = text.replace("\r", "").split("\n")
    meta = {"title": (L[0] if L else "").strip(), "attendees": "", "markers": []}
    att, sec = [], ""
    for raw in L[1:]:
        l = raw.strip()
        if not l:
            sec = ""
            continue
        if l.lower().startswith("attendees:"):
            sec = "att"; continue
        if l.lower().startswith("notes (time in the audio)"):
            sec = "notes"; continue
        if sec == "att" and l.startswith("- ") and "not recorded" not in l:
            att.append(l[2:].strip())
        if sec == "notes":
            m = _re.match(r"^-?\s*\[(\d\d):(\d\d):(\d\d)\]\s*(.*)$", l)
            a = _re.match(r"^-?\s*\[after meeting\]\s*(.*)$", l, _re.I)
            if m and m.group(4) and m.group(4) != "(none)":
                meta["markers"].append({"t": (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))) * 1000, "note": m.group(4)})
            elif a:
                meta["markers"].append({"t": None, "note": a.group(1)})
    meta["attendees"] = "\n".join(att)
    return meta


def meta_for(src: Path) -> dict | None:
    """Title, attendees and notes for a recording: embedded in a Field Notes WAV, or a sibling _notes.txt."""
    for cand in (src.with_name(src.stem + "_notes.txt"),):
        if cand.exists():
            try:
                return parse_notes_txt(cand.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass
    if src.suffix.lower() == ".wav":
        from inbox import read_wav_meta
        return read_wav_meta(src)
    return None
