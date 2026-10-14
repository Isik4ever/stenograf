"""Taligenkänning: VAD-chunkning + GigaAM-v3 (ryska) eller Whisper (övriga språk).

Allt körs lokalt. Ljud in som 16 kHz mono float32-tensor, ut som ord med tidsstämplar.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator

import torch

SR = 16000

# GigaAM accepterar max 25 s per anrop. Vi håller marginal.
MAX_CHUNK_S = 22.0
# Paus längre än så här startar alltid en ny chunk (bättre tidsstämplar, ingen tystnad i batchen).
MAX_GAP_S = 1.6
CHUNK_PAD_S = 0.15


@dataclass
class Word:
    text: str
    start: float
    end: float
    speaker: int | None = None
    conf: float | None = None  # modellens säkerhet 0–1 (lägsta token-sannolikheten i ordet)


@dataclass
class Chunk:
    start: int  # samples
    end: int


@dataclass
class ChunkResult:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


ProgressFn = Callable[[float], None]


def pick_device() -> torch.device:
    forced = os.environ.get("STENOGRAF_DEVICE")
    if forced:
        return torch.device(forced)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def load_wav(path: str) -> torch.Tensor:
    """Avkoda valfri ljud/videofil till 16 kHz mono float32 via ffmpeg."""
    cmd = [
        "ffmpeg", "-nostdin", "-threads", "0", "-i", path,
        "-f", "s16le", "-ac", "1", "-acodec", "pcm_s16le", "-ar", str(SR), "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-1:]
        raise RuntimeError(f"ffmpeg kunde inte läsa ljudet: {' '.join(tail)}")
    audio = torch.frombuffer(bytearray(proc.stdout), dtype=torch.int16)
    return audio.float() / 32768.0


# ---------------------------------------------------------------- VAD

_vad_lock = threading.Lock()
_vad_model = None


def _vad():
    global _vad_model
    with _vad_lock:
        if _vad_model is None:
            from silero_vad import load_silero_vad

            _vad_model = load_silero_vad()
        return _vad_model


def speech_chunks(wav: torch.Tensor, on_progress: ProgressFn | None = None) -> list[Chunk]:
    """Hitta tal med Silero VAD och slå ihop till chunks på högst MAX_CHUNK_S sekunder."""
    from silero_vad import get_speech_timestamps

    total = wav.shape[-1]
    if total == 0:
        return []
    stamps = get_speech_timestamps(
        wav,
        _vad(),
        sampling_rate=SR,
        threshold=0.4,
        min_speech_duration_ms=200,
        min_silence_duration_ms=250,
        max_speech_duration_s=MAX_CHUNK_S - 2 * CHUNK_PAD_S,
        speech_pad_ms=120,
        progress_tracking_callback=(lambda p: on_progress(p / 100.0)) if on_progress else None,
    )
    return merge_speech(stamps, total)


def merge_speech(stamps: list[dict], total: int) -> list[Chunk]:
    """Girig sammanslagning av VAD-segment: få anrop, men aldrig över gränsen eller över långa pauser."""
    max_len = int(MAX_CHUNK_S * SR)
    max_gap = int(MAX_GAP_S * SR)
    pad = int(CHUNK_PAD_S * SR)
    chunks: list[Chunk] = []
    for st in stamps:
        s, e = int(st["start"]), int(st["end"])
        if chunks:
            cur = chunks[-1]
            if s - cur.end <= max_gap and e - cur.start <= max_len - 2 * pad:
                cur.end = e
                continue
        chunks.append(Chunk(s, e))
    out = []
    for c in chunks:
        s = max(0, c.start - pad)
        e = min(total, c.end + pad)
        if e - s > max_len:  # säkerhetsnät, ska inte hända
            e = s + max_len
        if e - s >= int(0.2 * SR):
            out.append(Chunk(s, e))
    # Padding får inte skapa överlapp.
    for a, b in zip(out, out[1:]):
        if b.start < a.end:
            mid = (a.end + b.start) // 2
            a.end, b.start = mid, mid
    return out


# ---------------------------------------------------------------- engines


@torch.inference_mode()
def rnnt_decode_with_conf(dec, head, encoded: torch.Tensor, enc_len: torch.Tensor):
    """GigaAM:s giriga RNN-T-avkodning (samma logik som gigaam.decoding.RNNTGreedyDecoding),
    men vi sparar också sannolikheten för varje utsänd token. Returnerar (ids, frames, probs) per prov."""
    x = encoded.transpose(1, 2)
    B, T, _ = x.shape
    device = x.device
    hyps: list[list[int]] = [[] for _ in range(B)]
    frames: list[list[int]] = [[] for _ in range(B)]
    probs: list[list[float]] = [[] for _ in range(B)]
    last_label: list = [None] * B
    dec_state: list = [None] * B

    def emit(batch_idx: list[int], t: int, fresh: bool) -> list[int]:
        idx = torch.tensor(batch_idx, device=device, dtype=torch.long)
        f = x[idx, t : t + 1, :]
        if fresh:
            g, hidden = head.decoder.predict(None, None, batch_size=len(batch_idx))
        else:
            labels = torch.cat([last_label[i] for i in batch_idx], dim=0)
            state = dec._cat_states([dec_state[i] for i in batch_idx])
            g, hidden = head.decoder.predict(labels, state, batch_size=len(batch_idx))
        logp = head.joint.joint(f, g)[:, 0, 0, :]
        best, k = logp.max(dim=-1)
        emitted = k.ne(dec.blank_id)
        if not emitted.any():
            return []
        parts = dec._split_state(hidden)
        out = []
        for p in emitted.nonzero(as_tuple=False).squeeze(1).tolist():
            bi = batch_idx[p]
            hyps[bi].append(int(k[p]))
            frames[bi].append(t)
            probs[bi].append(float(best[p].float().exp()))
            last_label[bi] = k[p : p + 1].view(1, 1)
            dec_state[bi] = parts[p]
            out.append(bi)
        return out

    enc_len = enc_len.cpu()
    for t in range(T):
        active = (t < enc_len).nonzero(as_tuple=False).squeeze(1).tolist()
        for _ in range(dec.max_symbols):
            if not active:
                break
            fresh = [i for i in active if dec_state[i] is None]
            stateful = [i for i in active if dec_state[i] is not None]
            nxt = []
            if fresh:
                nxt.extend(emit(fresh, t, True))
            if stateful:
                nxt.extend(emit(stateful, t, False))
            if not nxt:
                break
            active = nxt
    return list(zip(hyps, frames, probs))


def tokens_to_words(tokenizer, ids: list[int], frames: list[int], probs: list[float], shift: float) -> list[Word]:
    """Som gigaam.timestamps_utils.frames_to_words, plus säkerhet per ord."""
    words: list[Word] = []
    chars: list[str] = []
    fr: list[int] = []
    pr: list[float] = []

    def commit():
        text = "".join(chars).strip()
        if text:
            words.append(Word(text, fr[0] * shift, (fr[-1] + 1) * shift, conf=round(min(pr), 3)))
        chars.clear(), fr.clear(), pr.clear()

    for tid, f, p in zip(ids, frames, probs):
        ch = tokenizer.id_to_str(tid)
        if ch.startswith("▁"):
            commit()
            ch = ch[1:]
        elif ch == " ":
            commit()
            continue
        chars.append(ch)
        fr.append(f)
        pr.append(p)
    commit()
    return words


class GigaAMEngine:
    """Sber GigaAM-v3 e2e: SOTA för ryska, ger skiljetecken och normaliserad text direkt."""

    key = "gigaam"
    label = "GigaAM-v3 (ryska)"
    languages = ("ru",)

    def __init__(self, model_name: str = "v3_e2e_rnnt"):
        import gigaam

        self.device = pick_device()
        self.model = gigaam.load_model(model_name, device=self.device, fp16_encoder=self.device.type != "cpu")
        self.lock = threading.Lock()

    @torch.inference_mode()
    def run(
        self,
        wav: torch.Tensor,
        chunks: list[Chunk],
        batch_size: int = 8,
        on_chunk: Callable[[ChunkResult, int, int], None] | None = None,
    ) -> list[ChunkResult]:
        m = self.model
        results: list[ChunkResult] = []
        n = len(chunks)
        with self.lock:
            for i in range(0, n, batch_size):
                batch = chunks[i : i + batch_size]
                lens = [c.end - c.start for c in batch]
                pad = torch.zeros(len(batch), max(lens))
                for j, c in enumerate(batch):
                    pad[j, : lens[j]] = wav[c.start : c.end]
                x = pad.to(m._device).to(m._dtype)
                lx = torch.tensor(lens, device=m._device)
                enc, enc_len = m.forward(x, lx)
                for j, (ids, frames, probs) in enumerate(rnnt_decode_with_conf(m.decoding, m.head, enc, enc_len)):
                    c = batch[j]
                    off = c.start / SR
                    shift = lens[j] / SR / int(enc_len[j])
                    words = tokens_to_words(m.decoding.tokenizer, ids, frames, probs, shift)
                    r = ChunkResult(
                        start=off,
                        end=c.end / SR,
                        text=m.decoding.tokenizer.decode(ids).strip(),
                        words=[Word(w.text, round(off + w.start, 3), round(off + w.end, 3), conf=w.conf) for w in words],
                    )
                    results.append(r)
                    if on_chunk:
                        on_chunk(r, len(results), n)
        return results


# Skarvar mellan chunkar med kortare paus än så här kontrolleras om med kontext.
BOUNDARY_GAP_S = 0.35
BOUNDARY_CTX_S = 6.0
_LETTERS = re.compile(r"[^\w]+")


def _letters(t: str) -> str:
    return _LETTERS.sub("", t.lower())


def fix_boundaries(engine: "GigaAMEngine", wav: torch.Tensor, results: list[ChunkResult]) -> int:
    """Varje chunk transkriberas för sig, så modellen sätter ofta punkt + versal vid skarven
    även mitt i en mening. Kör om ett fönster som spänner över skarven och låt modellen avgöra
    skiljetecken och versal för orden närmast skarven. Bokstäverna ändras aldrig."""
    pairs = [
        (a, b)
        for a, b in zip(results, results[1:])
        if a.words and b.words and b.start - a.end < BOUNDARY_GAP_S
    ]
    if not pairs:
        return 0
    windows = [
        Chunk(int(max(a.start, a.end - BOUNDARY_CTX_S) * SR), int(min(b.end, b.start + BOUNDARY_CTX_S) * SR))
        for a, b in pairs
    ]
    redo = engine.run(wav, windows)
    changed = 0
    for (a, b), w in zip(pairs, redo):
        if not w.words:
            continue
        last, first = a.words[-1], b.words[0]
        ca = min(w.words, key=lambda x: abs(x.start - last.start))
        cb = min(w.words, key=lambda x: abs(x.start - first.start))
        if _letters(ca.text) != _letters(last.text) or _letters(cb.text) != _letters(first.text):
            continue  # fönstret hörde något annat; rör inget
        if (last.text, first.text) != (ca.text, cb.text):
            last.text, first.text = ca.text, cb.text
            a.text = " ".join(x.text for x in a.words)
            b.text = " ".join(x.text for x in b.words)
            changed += 1
    return changed


class WhisperEngine:
    """faster-whisper large-v3-turbo: fallback för andra språk än ryska."""

    key = "whisper"
    label = "Whisper large-v3-turbo (alla språk)"
    languages = None

    def __init__(self, model: str = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 8)
        self.lock = threading.Lock()

    def run_full(
        self,
        wav: torch.Tensor,
        language: str | None,
        on_chunk: Callable[[ChunkResult, float], None] | None = None,
    ) -> tuple[list[ChunkResult], str]:
        total = wav.shape[-1] / SR or 1.0
        with self.lock:
            segments, info = self.model.transcribe(
                wav.numpy(),
                language=language,
                beam_size=5,
                word_timestamps=True,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            out = []
            for s in segments:
                r = ChunkResult(
                    start=s.start,
                    end=s.end,
                    text=s.text.strip(),
                    words=[
                        Word(w.word.strip(), w.start, w.end, conf=round(w.probability, 3))
                        for w in (s.words or [])
                        if w.word.strip()
                    ],
                )
                out.append(r)
                if on_chunk:
                    on_chunk(r, min(1.0, s.end / total))
        return out, info.language


_engines: dict[str, object] = {}
_engines_lock = threading.Lock()


def get_engine(key: str):
    with _engines_lock:
        if key not in _engines:
            _engines[key] = GigaAMEngine() if key == "gigaam" else WhisperEngine()
        return _engines[key]


def iter_words(results: list[ChunkResult]) -> Iterator[Word]:
    for r in results:
        if r.words:
            yield from r.words
        elif r.text:  # ingen ordnivå: sprid texten jämnt över chunken
            toks = r.text.split()
            step = (r.end - r.start) / max(1, len(toks))
            for k, t in enumerate(toks):
                yield Word(t, r.start + k * step, r.start + (k + 1) * step)
