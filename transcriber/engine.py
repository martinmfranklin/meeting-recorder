"""Field Notes Transcriber engine.

Transcribes an audio or video file and labels who spoke when, entirely on this computer.

  ASR:          faster-whisper (CTranslate2), default model large-v3-turbo, GPU if available
  Speaker ID:   sherpa-onnx offline speaker diarization
                (pyannote segmentation 3.0 + speaker embedding model, ONNX, no PyTorch)
  Merge:        each recognised word is assigned to the speaker talking at that moment,
                then consecutive words from the same speaker become one utterance.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import time
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

SR = 16000
GH = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEG_URL = f"{GH}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMB_NAME = "nemo_en_titanet_large.onnx"
EMB_URL = f"{GH}/speaker-recongition-models/{EMB_NAME}"
# Clustering thresholds: lower = more speakers. Auto uses the first; to find "at least N" speakers the next
# ones are tried in turn until N are found. Tested on AMI meeting excerpts, clean and through a simulated
# Teams-through-laptop-speakers path: the old single threshold (0.7) merged about 60% of people with someone
# else; 0.6 plus stepping down to the attendee count cut that to under 40%. Asking sherpa-onnx for an exact
# cluster count merged more (about 80%), so a count is always treated as a minimum. The tuning leans towards
# splitting one person in two, which Field Notes fixes by giving both the same name.
THRESHOLDS = (0.6, 0.5, 0.4, 0.3)
AUTO_THRESHOLD = THRESHOLDS[0]

Progress = Callable[[str, float], None]  # (stage, fraction 0..1)


def app_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".local", "share")
    d = Path(base) / "FieldNotesTranscriber"
    d.mkdir(parents=True, exist_ok=True)
    return d


def models_dir() -> Path:
    d = app_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _download(url: str, dest: Path, log=print) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"Downloading {url.rsplit('/', 1)[-1]} ...")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    tmp.replace(dest)


def ensure_diarization_models(log=print) -> tuple[Path, Path]:
    md = models_dir()
    seg_dir = md / "sherpa-onnx-pyannote-segmentation-3-0"
    seg = seg_dir / "model.onnx"
    if not seg.exists():
        arc = md / "seg.tar.bz2"
        _download(SEG_URL, arc, log)
        with tarfile.open(arc) as t:
            t.extractall(md)
        arc.unlink(missing_ok=True)
    emb = md / EMB_NAME
    if not emb.exists():
        _download(EMB_URL, emb, log)
    return seg, emb


def load_audio(path: str) -> np.ndarray:
    """Decode any audio/video file to 16 kHz mono float32 with PyAV.

    Done here rather than via faster_whisper.audio.decode_audio, which breaks with PyAV 19
    (it passes an open() option that PyAV 19 removed)."""
    import av
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=SR)
    parts = []
    with av.open(str(path)) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("This file has no audio track.")
        for frame in container.decode(stream):
            frame.pts = None  # let the resampler re-time frames (avoids errors on odd timestamps)
            for out in resampler.resample(frame):
                parts.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            parts.append(out.to_ndarray().reshape(-1))
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return (np.concatenate(parts).astype(np.float32) / 32768.0)


# ---------------------------------------------------------------- speech recognition

def _add_cuda_dll_dirs() -> None:
    """On Windows, pip-installed NVIDIA libraries put their DLLs in site-packages/nvidia/*/bin."""
    if os.name != "nt":
        return
    for p in sys.path:
        nv = Path(p) / "nvidia"
        if nv.is_dir():
            for b in nv.glob("*/bin"):
                try:
                    os.add_dll_directory(str(b))
                    os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")
                except OSError:
                    pass


class FasterWhisperASR:
    def __init__(self, model: str = "large-v3-turbo", device: str = "auto", log=print):
        _add_cuda_dll_dirs()
        from faster_whisper import WhisperModel
        import ctranslate2

        self.model_name = model
        want_gpu = device in ("auto", "cuda")
        has_gpu = False
        try:
            has_gpu = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            pass
        self.model = None
        if want_gpu and has_gpu:
            try:
                self.model = WhisperModel(model, device="cuda", compute_type="float16",
                                          download_root=str(models_dir() / "whisper"))
                self.device = "GPU"
            except Exception as e:  # missing cuDNN/cuBLAS etc.
                log(f"GPU not usable ({e}); using CPU")
        if self.model is None:
            threads = max(1, (os.cpu_count() or 4) - 1)
            self.model = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads,
                                      download_root=str(models_dir() / "whisper"))
            self.device = "CPU"

    def transcribe(self, audio: np.ndarray, prompt: str = "", language: Optional[str] = None,
                   progress: Optional[Progress] = None) -> tuple[list[dict], str]:
        duration = len(audio) / SR
        segments, info = self.model.transcribe(
            audio,
            language=language,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            word_timestamps=True,
            condition_on_previous_text=False,   # avoids repetition loops on long meetings
            initial_prompt=prompt or None,       # names and terms improve spelling
        )
        out = []
        for s in segments:
            words = [{"start": w.start, "end": w.end, "word": w.word} for w in (s.words or [])]
            out.append({"start": s.start, "end": s.end, "text": s.text.strip(), "words": words})
            if progress and duration:
                progress("Transcribing", min(1.0, s.end / duration))
        return out, info.language


class SherpaWhisperTestASR:
    """Small test-only backend (sherpa-onnx Whisper tiny). Same output shape, no word timings."""

    def __init__(self, model_dir: str, **_):
        import sherpa_onnx
        d = Path(model_dir)
        enc = next(d.glob("*-encoder.int8.onnx"))
        dec = next(d.glob("*-decoder.int8.onnx"))
        tok = next(d.glob("*-tokens.txt"))
        self.rec = sherpa_onnx.OfflineRecognizer.from_whisper(encoder=str(enc), decoder=str(dec), tokens=str(tok),
                                                             num_threads=2)
        self.model_name = d.name
        self.device = "CPU"

    @staticmethod
    def _chunks(audio, max_len=25 * SR):
        """Split on pauses (simple energy gate), so each chunk is one stretch of speech."""
        f = 320  # 20 ms
        n = len(audio) // f
        if n == 0:
            return []
        e = np.sqrt((audio[:n * f].reshape(n, f) ** 2).mean(1) + 1e-12)
        thr = max(np.percentile(e, 20) * 3, 1e-4)
        voiced = e > thr
        out, start, quiet = [], None, 0
        for i, v in enumerate(voiced):
            if v:
                if start is None:
                    start = i
                quiet = 0
            elif start is not None:
                quiet += 1
                if quiet >= 25 or (i - start) * f >= max_len:  # 0.5 s pause
                    out.append((start * f, (i - quiet + 1) * f))
                    start, quiet = None, 0
        if start is not None:
            out.append((start * f, n * f))
        return [(a, b) for a, b in out if b - a >= SR // 4]

    def transcribe(self, audio, prompt="", language=None, progress=None):
        out = []
        n = len(audio)
        for a, b in self._chunks(audio):
            st = self.rec.create_stream()
            st.accept_waveform(SR, audio[a:b])
            self.rec.decode_stream(st)
            text = st.result.text.strip()
            if text:
                out.append({"start": a / SR, "end": b / SR, "text": text, "words": []})
            if progress:
                progress("Transcribing", min(1.0, b / n))
        return out, "en"


# ---------------------------------------------------------------- speaker identification

class Diarizer:
    def __init__(self, threshold: float = AUTO_THRESHOLD, log=print):
        import sherpa_onnx
        seg, emb = ensure_diarization_models(log)
        threads = max(1, (os.cpu_count() or 4) - 1)
        self._cfg = lambda thr: sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg)),
                num_threads=threads),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb), num_threads=threads),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=thr),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        self._sherpa = sherpa_onnx
        self.threshold = threshold
        self.info = {"embedding": EMB_NAME.removesuffix(".onnx"), "threshold": threshold}

    def _once(self, audio, thr, progress, label):
        sd = self._sherpa.OfflineSpeakerDiarization(self._cfg(thr))
        err = []

        def cb(done: int, total: int) -> int:
            if progress and total and not err:
                try:
                    progress(label, done / total)
                except BaseException as ex:  # do not raise through native code
                    err.append(ex)
            return 0

        result = sd.process(audio, callback=cb).sort_by_start_time()
        if err:
            raise err[0]
        return [{"start": r.start, "end": r.end, "speaker": int(r.speaker)} for r in result]

    def steps(self, at_least: int = 0) -> list[float]:
        """Thresholds to try: the auto one, then lower ones while fewer than at_least speakers are found."""
        steps = [t for t in THRESHOLDS if t <= self.threshold] or [self.threshold]
        return steps if at_least > 1 else steps[:1]

    def run(self, audio: np.ndarray, threshold: Optional[float] = None, progress: Optional[Progress] = None,
            label: str = "Identifying speakers") -> list[dict]:
        thr = self.threshold if threshold is None else threshold
        self.info["threshold"] = thr
        return self._once(audio, thr, progress, label)


# ---------------------------------------------------------------- merge

def _speaker_at(t0: float, t1: float, turns: list[dict]) -> Optional[int]:
    """Speaker with the most overlap in [t0, t1]; if none overlaps, the nearest turn."""
    best, best_ov = None, 0.0
    for tr in turns:
        if tr["end"] < t0:
            continue
        if tr["start"] > t1:
            break
        ov = min(t1, tr["end"]) - max(t0, tr["start"])
        if ov > best_ov:
            best, best_ov = tr["speaker"], ov
    if best is not None:
        return best
    if not turns:
        return None
    mid = (t0 + t1) / 2
    near = min(turns, key=lambda tr: 0 if tr["start"] <= mid <= tr["end"] else min(abs(tr["start"] - mid), abs(tr["end"] - mid)))
    return near["speaker"]


def merge(segments: list[dict], turns: list[dict], join_gap: float = 1.5) -> list[dict]:
    turns = sorted(turns, key=lambda t: t["start"])
    units = []  # (start, end, text, speaker)
    for s in segments:
        if s.get("words"):
            for w in s["words"]:
                units.append((w["start"], w["end"], w["word"], _speaker_at(w["start"], w["end"], turns)))
        else:
            units.append((s["start"], s["end"], " " + s["text"], _speaker_at(s["start"], s["end"], turns)))
    utts: list[dict] = []
    for st, en, tx, sp in units:
        if utts and utts[-1]["speaker"] == sp and st - utts[-1]["end"] <= join_gap:
            utts[-1]["end"] = en
            utts[-1]["text"] += tx
        else:
            utts.append({"start": st, "end": en, "speaker": sp, "text": tx})
    for u in utts:
        u["text"] = " ".join(u["text"].split())
        u["start"] = round(u["start"], 2)
        u["end"] = round(u["end"], 2)
    return [u for u in utts if u["text"]]


def speaker_summary(utts: list[dict]) -> list[dict]:
    """Relabel speakers S1, S2... in order of first appearance; give talk time and sample ranges."""
    order: list = []
    for u in utts:
        if u["speaker"] not in order:
            order.append(u["speaker"])
    label = {sp: f"S{i + 1}" for i, sp in enumerate(order)}
    for u in utts:
        u["speaker"] = label.get(u["speaker"], "S?")
    out = []
    for sp in order:
        lab = label[sp]
        mine = [u for u in utts if u["speaker"] == lab]
        talk = sum(u["end"] - u["start"] for u in mine)
        samples = sorted(mine, key=lambda u: u["end"] - u["start"], reverse=True)[:3]
        out.append({
            "id": lab,
            "talk_sec": round(talk, 1),
            "samples": [{"start": u["start"], "end": min(u["end"], u["start"] + 10), "text": u["text"][:160]} for u in samples],
        })
    return out


@dataclass
class Job:
    path: str
    title: str = ""
    attendees: list = None
    num_speakers: int = 0    # how many people spoke, if known (treated as "at least")
    language: Optional[str] = None
    reuse: bool = False      # reuse this recording's earlier transcript text; only redo speakers
    min_speakers: int = 0    # on auto, find at least this many (the attendee count)


# ---------------------------------------------------------------- transcript cache
# Word timings from a finished transcription are kept for CACHE_DAYS, keyed by the decoded audio, so
# "Re-identify speakers" redoes only the speaker step (about a quarter of the time) instead of everything.

CACHE_DAYS = 14


def cache_dir() -> Path:
    d = app_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def audio_key(audio: np.ndarray, model: str) -> str:
    h = hashlib.sha256(audio.tobytes()).hexdigest()[:32]
    return f"{h}-{model or 'asr'}".replace("/", "_")


def cache_get(key: str):
    f = cache_dir() / f"{key}.json.gz"
    if not f.exists():
        return None
    try:
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        os.utime(f)  # keep recently used entries longer
        return d["segments"], d["language"]
    except Exception:
        return None


def cache_put(key: str, segments: list, language: str) -> None:
    d = cache_dir()
    try:
        tmp = d / f"{key}.part"
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump({"segments": segments, "language": language}, fh)
        tmp.replace(d / f"{key}.json.gz")
    except Exception:
        pass
    cutoff = time.time() - CACHE_DAYS * 86400
    for f in d.glob("*.json.gz"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def build_prompt(title: str, attendees: list) -> str:
    names = [a.split(",")[0].strip() for a in (attendees or []) if a.strip()]
    parts = []
    if title:
        parts.append(f"Meeting: {title}.")
    if names:
        parts.append("Attendees: " + ", ".join(names) + ".")
    return " ".join(parts)


def run_job(job: Job, asr, diarizer: Diarizer, progress: Progress) -> dict:
    progress("Reading audio", 0.0)
    audio = load_audio(job.path)
    duration = len(audio) / SR
    key = audio_key(audio, getattr(asr, "model_name", ""))
    cached = cache_get(key) if job.reuse else None
    # weight the stages so one progress bar moves smoothly: speakers ~25%, words ~75%
    # (when the words are reused, the speaker step is the whole job)
    w = 1.0 if cached else 0.25
    progress("Identifying speakers", 0.0)
    # a typed speaker count, or else the attendee count, is a minimum: fewer found means people were merged
    at_least = min(20, job.num_speakers or job.min_speakers or 0)
    steps = diarizer.steps(at_least)
    turns = diarizer.run(audio, steps[0], lambda st, f: progress(st, w * f))
    if cached:
        segs, lang = cached
    else:
        segs, lang = asr.transcribe(audio, build_prompt(job.title, job.attendees), job.language,
                                    lambda st, f: progress(st, 0.25 + 0.75 * f))
        cache_put(key, segs, lang)
    utts = merge(segs, turns)
    for thr in steps[1:]:
        # count people who actually have words, since that is what the transcript shows
        if len({u["speaker"] for u in utts}) >= at_least:
            break
        turns = diarizer.run(audio, thr, lambda st, f: progress(st, 0.9 + 0.1 * f), f"Looking for {at_least} speakers")
        utts = merge(segs, turns)
    speakers = speaker_summary(utts)
    progress("Done", 1.0)
    return {
        "format": "field-notes-transcript/1",
        "duration": round(duration, 2),
        "language": lang,
        "model": getattr(asr, "model_name", ""),
        "device": getattr(asr, "device", ""),
        "diarization": dict(getattr(diarizer, "info", {}), at_least=at_least or "auto", reused_words=bool(cached)),
        "speakers": speakers,
        "utterances": utts,
    }
