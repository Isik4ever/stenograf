"""Vem talar när: pyannote-segmentering + WeSpeaker-röstavtryck via sherpa-onnx. Lokalt, ingen token."""

from __future__ import annotations

import os
import pathlib
import tarfile
import threading
import urllib.request
from typing import Callable

import numpy as np
import torch

from .asr import SR, Word

CACHE = pathlib.Path.home() / ".cache" / "stenograf"
_REL = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEG_URL = f"{_REL}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMB_URL = f"{_REL}/speaker-recongition-models/nemo_en_titanet_small.onnx"

# Högre = färre talare. TitaNet + 0.7 skilde två liknande kvinnoröster i ryska testintervjun;
# WeSpeaker och 3D-Speaker slog ihop dem eller splittrade i tiotals kluster.
CLUSTER_THRESHOLD = float(os.environ.get("STENOGRAF_SPEAKER_THRESHOLD", "0.7"))
# Kluster med mindre tal än så här är nästan alltid brus/skratt/överlapp och slås ihop med grannen.
MIN_SPEAKER_S = 30.0
MIN_SPEAKER_SHARE = 0.025

_lock = threading.Lock()
_sd = None
_ext = None


def _fetch(url: str, dest: pathlib.Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.replace(dest)


def _models() -> tuple[pathlib.Path, pathlib.Path]:
    CACHE.mkdir(parents=True, exist_ok=True)
    seg = CACHE / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
    if not seg.exists():
        arc = CACHE / "seg.tar.bz2"
        _fetch(SEG_URL, arc)
        with tarfile.open(arc) as t:
            t.extractall(CACHE, filter="data")
        arc.unlink()
    emb = CACHE / "nemo_en_titanet_small.onnx"
    if not emb.exists():
        _fetch(EMB_URL, emb)
    return seg, emb


def _engine():
    global _sd
    with _lock:
        if _sd is None:
            import sherpa_onnx as so

            seg, emb = _models()
            # Halva kärnorna: GigaAM avkodar på CPU samtidigt, så vi svälter inte den.
            threads = max(2, (os.cpu_count() or 8) // 2)
            cfg = so.OfflineSpeakerDiarizationConfig(
                segmentation=so.OfflineSpeakerSegmentationModelConfig(
                    pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg)),
                    num_threads=threads,
                ),
                embedding=so.SpeakerEmbeddingExtractorConfig(model=str(emb), num_threads=threads),
                clustering=so.FastClusteringConfig(num_clusters=-1, threshold=CLUSTER_THRESHOLD),
                min_duration_on=0.3,
                min_duration_off=0.5,
            )
            if not cfg.validate():
                raise RuntimeError("Talarmodellerna kunde inte laddas.")
            _sd = so.OfflineSpeakerDiarization(cfg)
        return _sd


def _extractor():
    global _ext
    with _lock:
        if _ext is None:
            import sherpa_onnx as so

            _, emb = _models()
            _ext = so.SpeakerEmbeddingExtractor(
                so.SpeakerEmbeddingExtractorConfig(model=str(emb), num_threads=max(2, (os.cpu_count() or 8) // 2))
            )
        return _ext


def _embed(wav: torch.Tensor, a: float, b: float) -> np.ndarray:
    """Röstavtryck för [a, b]; långa segment kortas till mittersta 15 s (räcker gott, går snabbt)."""
    if b - a > 15.0:
        mid = (a + b) / 2
        a, b = mid - 7.5, mid + 7.5
    ext = _extractor()
    st = ext.create_stream()
    st.accept_waveform(SR, wav[int(a * SR) : int(b * SR)].numpy().astype("float32"))
    st.input_finished()
    e = np.asarray(ext.compute(st), dtype=np.float32)
    return e / (np.linalg.norm(e) + 1e-9)


def _centroid(wav: torch.Tensor, segs: list[tuple[float, float, int]], idx: list[int]) -> np.ndarray:
    """Varaktighetsviktat medel av röstavtrycken från klustrets (högst 8) längsta segment."""
    top = sorted(idx, key=lambda i: segs[i][1] - segs[i][0], reverse=True)[:8]
    vs = [_embed(wav, segs[i][0], segs[i][1]) * min(15.0, segs[i][1] - segs[i][0]) for i in top]
    c = np.sum(vs, axis=0)
    return c / (np.linalg.norm(c) + 1e-9)


def _split2(x: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Sfärisk 2-means, startad från de två mest olika segmenten."""
    sim = x @ x.T
    a, b = np.unravel_index(np.argmin(sim), sim.shape)
    c = np.stack([x[a], x[b]])
    lab = np.zeros(len(x), dtype=int)
    for _ in range(15):
        lab = np.argmax(x @ c.T, axis=1)
        for k in (0, 1):
            if (lab == k).any():
                m = (x[lab == k] * w[lab == k, None]).sum(0)
                c[k] = m / (np.linalg.norm(m) + 1e-9)
    return lab


def to_n_speakers(wav: torch.Tensor, segs: list[tuple[float, float, int]], n: int) -> list[tuple[float, float, int]]:
    """Utgå från den automatiska klustringen och justera till exakt n talare:
    för många → minsta talaren går till den mest lika rösten; för få → dela den största."""
    segs = list(segs)
    groups: dict[int, list[int]] = {}
    for i, (_, _, spk) in enumerate(segs):
        groups.setdefault(spk, []).append(i)

    def dur(g: int) -> float:
        return sum(segs[i][1] - segs[i][0] for i in groups[g])

    cents = {g: _centroid(wav, segs, ix) for g, ix in groups.items()}

    while len(groups) > n:
        small = min(groups, key=dur)
        target = max((g for g in groups if g != small), key=lambda g: float(cents[g] @ cents[small]))
        groups[target] += groups.pop(small)
        cents[target] = _centroid(wav, segs, groups[target])
        cents.pop(small)

    next_id = max(groups) + 1 if groups else 0
    while groups and len(groups) < n:
        big = max(groups, key=dur)
        ix = [i for i in groups[big] if segs[i][1] - segs[i][0] >= 1.5]
        if len(ix) < 4:
            break
        x = np.stack([_embed(wav, segs[i][0], segs[i][1]) for i in ix])
        w = np.array([min(15.0, segs[i][1] - segs[i][0]) for i in ix])
        lab = _split2(x, w)
        part = {i for i, l in zip(ix, lab) if l == 1}
        if sum(segs[i][1] - segs[i][0] for i in part) < 0.05 * dur(big):
            break  # ingen verklig andra röst i klustret
        groups[big] = [i for i in groups[big] if i not in part]
        groups[next_id] = sorted(part)
        cents[big] = _centroid(wav, segs, groups[big])
        cents[next_id] = _centroid(wav, segs, groups[next_id])
        next_id += 1

    label = {i: g for g, ix in groups.items() for i in ix}
    return cleanup([(a, b, label[i]) for i, (a, b, _) in enumerate(segs)], keep_all=True)


def diarize(
    wav: torch.Tensor,
    on_progress: Callable[[float], None] | None = None,
    num_speakers: int | None = None,
) -> list[tuple[float, float, int]]:
    """Returnerar [(start, slut, talare)] sorterat på tid. Talarnummer i ordningen de först hörs."""
    sd = _engine()

    def cb(done: int, total: int) -> int:
        if on_progress and total:
            on_progress(done / total)
        return 0

    result = sd.process(wav.numpy().astype("float32"), callback=cb).sort_by_start_time()
    raw = [(s.start, s.end, s.speaker) for s in result]
    if num_speakers and num_speakers > 1:
        # Känt antal: sherpas egen "exakt N" låter ett enda brussegment bli en talare,
        # så vi klustrar om röstavtrycken själva.
        return to_n_speakers(wav, raw, num_speakers)
    return cleanup(raw)


def cleanup(segs: list[tuple[float, float, int]], keep_all: bool = False) -> list[tuple[float, float, int]]:
    """Slå ihop småkluster med närmaste riktiga talare och numrera i ordningen de först hörs."""
    if not segs:
        return []
    total: dict[int, float] = {}
    for a, b, spk in segs:
        total[spk] = total.get(spk, 0.0) + (b - a)
    speech = sum(total.values()) or 1.0
    keep = set(total) if keep_all else {
        k for k, v in total.items() if v >= MIN_SPEAKER_S or v / speech >= MIN_SPEAKER_SHARE
    }
    if not keep:
        keep = {max(total, key=total.get)}
    kept = [(a, b, s) for a, b, s in segs if s in keep]
    fixed = []
    for a, b, s in segs:
        if s not in keep:
            mid = (a + b) / 2
            s = min(kept, key=lambda k: max(k[0] - mid, mid - k[1], 0.0))[2]
        fixed.append((a, b, s))
    order: dict[int, int] = {}
    for _, _, spk in fixed:
        order.setdefault(spk, len(order))
    # Slå ihop direkt angränsande segment med samma talare.
    out: list[tuple[float, float, int]] = []
    for a, b, s in fixed:
        s = order[s]
        if out and out[-1][2] == s and a - out[-1][1] < 0.6:
            out[-1] = (out[-1][0], max(out[-1][1], b), s)
        else:
            out.append((a, b, s))
    return out


def assign(words: list[Word], segs: list[tuple[float, float, int]]) -> list[int | None]:
    """Talare per ord: störst tidsöverlapp, annars närmaste segment inom 1 s."""
    out: list[int | None] = []
    j = 0
    n = len(segs)
    for w in words:
        while j < n and segs[j][1] < w.start - 1.0:
            j += 1
        best, best_ov, best_dist = None, 0.0, 1.0
        k = j
        while k < n and segs[k][0] <= w.end + 1.0:
            a, b, spk = segs[k]
            ov = min(b, w.end) - max(a, w.start)
            if ov > best_ov:
                best, best_ov = spk, ov
            elif best_ov <= 0:
                dist = max(a - w.end, w.start - b, 0.0)
                if dist < best_dist:
                    best, best_dist = spk, dist
            k += 1
        out.append(best)
    return out
