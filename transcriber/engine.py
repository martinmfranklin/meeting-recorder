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
EMB_NAME = "nemo_en_titanet_small.onnx"
EMB_URL = f"{GH}/speaker-recongition-models/{EMB_NAME}"
# Speaker identification runs in two steps.
#  1. sherpa-onnx finds speech turns and an initial grouping (its threshold only needs to keep different
#     people apart; on long recordings it splits one person into dozens of fragments, which step 2 fixes).
#  2. Every turn gets a voice embedding and the turns are regrouped here with Ward clustering into exactly
#     the number of speakers asked for, or an estimate when no number is given.
# Step 1 and the embeddings are cached with the words, so Re-identify with another number takes seconds.
STAGE1_THRESHOLD = 0.7
MIN_TURN = 1.5        # seconds; shorter turns are too short for a reliable voice match and follow the others
MAX_AUTO = 8          # upper limit when estimating the number of speakers

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

def ward_levels(X: np.ndarray, kmin: int, kmax: int) -> dict:
    """Ward agglomerative clustering of the rows of X, numpy only (matches scipy's 'ward').
    Returns {k: labels} for each k from kmin to kmax."""
    n = len(X)
    out: dict = {}
    if n == 0:
        return out
    lab = np.arange(n)
    if n <= kmax:
        out[n] = lab.copy()
    X = X.astype(np.float64)
    sq = (X * X).sum(1)
    D = sq[:, None] + sq[None, :] - 2 * X @ X.T
    np.maximum(D, 0, out=D)
    np.fill_diagonal(D, np.inf)
    size = np.ones(n)
    alive = np.ones(n, bool)
    k = n
    while k > max(1, kmin):
        i, j = divmod(int(np.argmin(D)), n)
        if i > j:
            i, j = j, i
        si, sj, dij = size[i], size[j], D[i, j]
        new = ((si + size) * D[i] + (sj + size) * D[j] - size * dij) / (si + sj + size)
        new[~alive] = np.inf
        new[i] = np.inf
        D[i, :] = new
        D[:, i] = new
        D[j, :] = np.inf
        D[:, j] = np.inf
        alive[j] = False
        size[i] = si + sj
        lab[lab == j] = i
        k -= 1
        if k <= kmax:
            out[k] = lab.copy()
    return out


def _separation(X: np.ndarray, lab: np.ndarray) -> float:
    """Mean of (similarity to own group) minus (similarity to the closest other group); higher is cleaner."""
    groups = list(set(lab.tolist()))
    if len(groups) < 2:
        return -1.0
    C = X @ X.T
    M = np.stack([C[:, lab == g].mean(1) for g in groups], 1)   # mean similarity of each turn to each group
    gi = np.array([groups.index(v) for v in lab])
    own = M[np.arange(len(X)), gi]
    M[np.arange(len(X)), gi] = -np.inf
    return float((own - M.max(1)).mean())


class Diarizer:
    def __init__(self, threshold: float = STAGE1_THRESHOLD, log=print):
        import sherpa_onnx
        seg, emb = ensure_diarization_models(log)
        threads = max(1, (os.cpu_count() or 4) - 1)
        self._sd = sherpa_onnx.OfflineSpeakerDiarization(sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg)),
                num_threads=threads),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb), num_threads=threads),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=threshold),
            min_duration_on=0.3,
            min_duration_off=0.5,
        ))
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb), num_threads=threads))
        self.name = EMB_NAME.removesuffix(".onnx")

    def analyse(self, audio: np.ndarray, progress: Optional[Progress] = None) -> dict:
        """Speech turns with one voice embedding each (None for turns too short to measure). Cacheable."""
        err = []

        def cb(done: int, total: int) -> int:
            if progress and total and not err:
                try:
                    progress("Identifying speakers", 0.85 * done / total)
                except BaseException as ex:  # do not raise through native code
                    err.append(ex)
            return 0

        result = self._sd.process(audio, callback=cb).sort_by_start_time()
        if err:
            raise err[0]
        turns = [[round(r.start, 3), round(r.end, 3), int(r.speaker)] for r in result]
        embs = []
        for k, (s, e, _) in enumerate(turns):
            vs, a = [], s
            while e - a >= 0.8:                      # mean of up to 3 s windows across the turn
                b = min(e, a + 3.0)
                if e - b < 0.8:
                    b = e
                st = self._ex.create_stream()
                st.accept_waveform(SR, audio[int(a * SR):int(b * SR)])
                st.input_finished()
                if self._ex.is_ready(st):
                    v = np.asarray(self._ex.compute(st), dtype=np.float32)
                    vs.append(v / (np.linalg.norm(v) + 1e-9))
                a = b
            if vs:
                m = np.mean(vs, 0)
                embs.append([round(float(x), 5) for x in m / (np.linalg.norm(m) + 1e-9)])
            else:
                embs.append(None)
            if progress and k % 20 == 0:
                progress("Identifying speakers", 0.85 + 0.15 * k / max(1, len(turns)))
        return {"model": self.name, "turns": turns, "emb": embs}

    @staticmethod
    def assign(an: dict, num_speakers: int = 0) -> tuple[list[dict], dict]:
        """Group the turns into num_speakers people (or an estimate). Returns turns and a summary."""
        turns, embs = an["turns"], an["emb"]
        if not turns:
            return [], {"speakers": 0, "estimated": not num_speakers}
        idx = [i for i, (s, e, _) in enumerate(turns) if embs[i] is not None and e - s >= MIN_TURN]
        if len(idx) < 2:
            idx = [i for i in range(len(turns)) if embs[i] is not None]
        if not idx:
            return [{"start": s, "end": e, "speaker": 0} for s, e, _ in turns], {"speakers": 1, "estimated": not num_speakers}
        X = np.array([embs[i] for i in idx], dtype=np.float64)
        if num_speakers:
            k = max(1, min(num_speakers, len(idx)))
            lab = ward_levels(X, k, k)[k] if k < len(idx) else np.arange(len(idx))
        else:
            levels = ward_levels(X, 1, min(MAX_AUTO, len(idx)))
            k = max((kk for kk in levels if kk >= 2), key=lambda kk: _separation(X, levels[kk]), default=1)
            lab = levels[k]
        groups = sorted(set(lab.tolist()))
        C = np.stack([X[lab == g].mean(0) for g in groups])
        C /= np.linalg.norm(C, axis=1, keepdims=True)
        out = [None] * len(turns)
        for n, i in enumerate(idx):
            out[i] = groups.index(int(lab[n]))
        for i in range(len(turns)):
            if out[i] is None and embs[i] is not None and turns[i][1] - turns[i][0] >= 0.8:
                out[i] = int(np.argmax(C @ np.asarray(embs[i])))
        known = [i for i in range(len(turns)) if out[i] is not None]
        for i in range(len(turns)):
            if out[i] is None:   # too short to measure: same person as the nearest measured turn in time
                j = min(known, key=lambda j: abs(turns[j][0] - turns[i][0]))
                out[i] = out[j]
        return ([{"start": s, "end": e, "speaker": out[i]} for i, (s, e, _) in enumerate(turns)],
                {"speakers": len(groups), "estimated": not num_speakers})


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
    min_speakers: int = 0    # accepted for older Field Notes versions; no longer used


# ---------------------------------------------------------------- transcript cache
# Word timings and the speaker analysis (turn times and voice embeddings, no audio) are kept for
# CACHE_DAYS, keyed by the decoded audio, so "Re-identify speakers" only regroups the turns.

CACHE_DAYS = 14


def cache_dir() -> Path:
    d = app_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def audio_key(audio: np.ndarray) -> str:
    return hashlib.sha256(audio.tobytes()).hexdigest()[:32]


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def cache_get(key: str, kind: str):
    f = cache_dir() / f"{key}-{_safe(kind)}.json.gz"
    if not f.exists():
        return None
    try:
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        os.utime(f)  # keep recently used entries longer
        return d
    except Exception:
        return None


def cache_put(key: str, kind: str, data) -> None:
    d = cache_dir()
    try:
        tmp = d / f"{key}-{_safe(kind)}.part"
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(data, fh)
        tmp.replace(d / f"{key}-{_safe(kind)}.json.gz")
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
    key = audio_key(audio)
    words_kind = "words-" + (getattr(asr, "model_name", "") or "asr")
    spk_kind = "speakers-" + diarizer.name
    words = cache_get(key, words_kind) if job.reuse else None
    an = cache_get(key, spk_kind) if job.reuse else None
    # progress: speaker analysis ~25%, words ~75%; whatever is reused is skipped
    w = 0.25 if not words else 1.0
    progress("Identifying speakers", 0.0)
    if not an:
        an = diarizer.analyse(audio, lambda st, f: progress(st, w * f))
        cache_put(key, spk_kind, an)
    turns, info = diarizer.assign(an, min(20, max(0, job.num_speakers)))
    if words:
        segs, lang = words["segments"], words["language"]
    else:
        segs, lang = asr.transcribe(audio, build_prompt(job.title, job.attendees), job.language,
                                    lambda st, f: progress(st, 0.25 + 0.75 * f))
        cache_put(key, words_kind, {"segments": segs, "language": lang})
    utts = merge(segs, turns)
    speakers = speaker_summary(utts)
    progress("Done", 1.0)
    return {
        "format": "field-notes-transcript/1",
        "duration": round(duration, 2),
        "language": lang,
        "model": getattr(asr, "model_name", ""),
        "device": getattr(asr, "device", ""),
        "diarization": {"embedding": diarizer.name, "requested": job.num_speakers or "auto",
                        "estimated": info["estimated"], "reused": bool(words)},
        "speakers": speakers,
        "utterances": utts,
    }
