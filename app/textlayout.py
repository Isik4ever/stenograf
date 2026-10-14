"""Ord med tidsstämplar -> meningar -> stycken (läsvy) och undertextrutor (SRT/VTT)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .asr import Word

_END = re.compile(r"[.!?…]+[»\"')\]]*$")
_SOFT = re.compile(r"[,;:—–]+[»\"')\]]*$")

# Läsvy
PARA_PAUSE_S = 2.2
PARA_MAX_SENTENCES = 7
PARA_MAX_CHARS = 700
SENT_MAX_S = 30.0  # okommaterad monolog: tvinga radbrytning

# Undertexter (Netflix-liknande riktlinjer)
CUE_MAX_CHARS = 84  # 2 rader à 42
CUE_LINE = 42
CUE_MAX_S = 7.0
CUE_MIN_S = 0.9


@dataclass
class Sentence:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    speaker: int | None = None


@dataclass
class Paragraph:
    start: float
    end: float
    sentences: list[Sentence]

    @property
    def speaker(self) -> int | None:
        return self.sentences[0].speaker if self.sentences else None

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.sentences)


@dataclass
class Cue:
    start: float
    end: float
    text: str  # kan innehålla \n


def _join(words: list[Word]) -> str:
    return " ".join(w.text for w in words).strip()


def _majority(words: list[Word]) -> int | None:
    votes: dict[int, float] = {}
    for w in words:
        if w.speaker is not None:
            votes[w.speaker] = votes.get(w.speaker, 0.0) + (w.end - w.start) + 0.05
    return max(votes, key=votes.get) if votes else None


def smooth_speakers(words: list[Word], min_run: int = 3) -> None:
    """Korta talarbyten (1–2 ord inklämda mellan samma talare) är nästan alltid brus."""
    n = len(words)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and words[j + 1].speaker == words[i].speaker:
            j += 1
        before = words[i - 1].speaker if i > 0 else None
        after = words[j + 1].speaker if j + 1 < n else None
        if j - i + 1 < min_run and before is not None and (before == after or after is None):
            for k in range(i, j + 1):
                words[k].speaker = before
        i = j + 1


def sentences(words: list[Word]) -> list[Sentence]:
    out: list[Sentence] = []
    cur: list[Word] = []

    def flush():
        if cur:
            out.append(Sentence(cur[0].start, cur[-1].end, _join(cur), list(cur), _majority(cur)))
            cur.clear()

    prev: Word | None = None
    for w in words:
        if cur and prev is not None:
            gap = w.start - prev.end
            # Lång paus mitt i en mening (komma + liten bokstav) bryter inte meningen.
            continues = w.text[:1].islower() and not _END.search(prev.text)
            long_pause = gap > PARA_PAUSE_S and not continues
            # Ny talare = ny mening, även om modellen inte satt punkt.
            new_speaker = w.speaker is not None and prev.speaker is not None and w.speaker != prev.speaker
            too_long = w.end - cur[0].start > SENT_MAX_S and (_SOFT.search(prev.text) or gap > 0.5)
            if new_speaker or long_pause or too_long:
                flush()
        cur.append(w)
        if _END.search(w.text):
            flush()
        prev = w
    flush()
    return out


def paragraphs(sents: list[Sentence]) -> list[Paragraph]:
    out: list[Paragraph] = []
    cur: list[Sentence] = []
    chars = 0
    for s in sents:
        if cur:
            gap = s.start - cur[-1].end
            new_speaker = s.speaker is not None and s.speaker != cur[-1].speaker
            if new_speaker or gap > PARA_PAUSE_S or len(cur) >= PARA_MAX_SENTENCES or chars + len(s.text) > PARA_MAX_CHARS:
                out.append(Paragraph(cur[0].start, cur[-1].end, cur))
                cur, chars = [], 0
        cur.append(s)
        chars += len(s.text) + 1
    if cur:
        out.append(Paragraph(cur[0].start, cur[-1].end, cur))
    return out


def _fits(text: str) -> bool:
    """Ryms texten på högst två rader à CUE_LINE tecken?"""
    if len(text) <= CUE_LINE:
        return True
    if len(text) > CUE_MAX_CHARS:
        return False
    return any(
        m.start() <= CUE_LINE and len(text) - m.end() <= CUE_LINE for m in re.finditer(" ", text)
    )


def _wrap(text: str) -> str:
    """Bryt i två så jämna rader som möjligt, gärna efter skiljetecken."""
    if len(text) <= CUE_LINE:
        return text
    best, best_score = None, None
    for m in re.finditer(" ", text):
        a, b = text[: m.start()], text[m.end() :]
        longest = max(len(a), len(b))
        score = longest * 2 + (40 if longest > CUE_LINE else 0) - (8 if _SOFT.search(a) or _END.search(a) else 0)
        if best_score is None or score < best_score:
            best, best_score = (a, b), score
    return "\n".join(best) if best else text


def cues(sents: list[Sentence]) -> list[Cue]:
    out: list[Cue] = []
    for s in sents:
        ws = s.words or [Word(s.text, s.start, s.end)]
        cur: list[Word] = []
        for i, w in enumerate(ws):
            cand = cur + [w]
            if cur and (not _fits(_join(cand)) or w.end - cur[0].start > CUE_MAX_S):
                # Backa till senaste naturliga brytpunkt om det finns en i andra halvan.
                cut = len(cur)
                for k in range(len(cur) - 1, len(cur) // 2, -1):
                    if _SOFT.search(cur[k].text):
                        cut = k + 1
                        break
                out.append(Cue(cur[0].start, cur[cut - 1].end, _wrap(_join(cur[:cut]))))
                cur = cur[cut:]
            cur.append(w)
        if cur:
            out.append(Cue(cur[0].start, cur[-1].end, _wrap(_join(cur))))
    # Minsta visningstid, och aldrig överlapp (ordtider från grannchunkar kan gå omlott).
    for a, b in zip(out, out[1:] + [None]):
        if a.end - a.start < CUE_MIN_S:
            limit = b.start if b else a.start + CUE_MIN_S
            a.end = max(a.end, min(a.start + CUE_MIN_S, limit))
        if b is not None and a.end > b.start:
            a.end = max(a.start + 0.05, b.start)
            if b.start < a.end:
                b.start = a.end
    return out


def retime(sent: dict, text: str) -> list:
    """Behåll ordtider där orden matchar, fördela resten jämnt (godt nog för undertexter)."""
    old = sent.get("words") or []
    new = text.split()
    if [w[0] for w in old] == new:
        return old
    if len(old) == len(new):
        return [[t, w[1], w[2]] for t, w in zip(new, old)]
    start, end = sent["start"], sent["end"]
    step = (end - start) / max(1, len(new))
    return [[t, round(start + i * step, 3), round(start + (i + 1) * step, 3)] for i, t in enumerate(new)]


def layout(words: list[Word]) -> dict:
    """Hela strukturen som JSON-vänlig dict (sparas per jobb)."""
    if any(w.speaker is not None for w in words):
        smooth_speakers(words)
    sents = sentences(words)
    paras = paragraphs(sents)
    return {
        "paragraphs": [
            {
                "start": round(p.start, 3),
                "end": round(p.end, 3),
                "speaker": p.speaker,
                "sentences": [
                    {
                        "start": round(s.start, 3),
                        "end": round(s.end, 3),
                        "text": s.text,
                        "speaker": s.speaker,
                        "words": [[w.text, round(w.start, 3), round(w.end, 3), w.conf] for w in s.words],
                    }
                    for s in p.sentences
                ],
            }
            for p in paras
        ],
        "cues": [asdict(c) for c in cues(sents)],
    }


def words_from_transcript(transcript: dict) -> list[Word]:
    """Tillbaka till ordlistan (med talare och säkerhet) för att kunna lägga om layouten."""
    out = []
    for p in transcript.get("paragraphs", []):
        for s in p["sentences"]:
            spk = s.get("speaker", p.get("speaker"))
            for w in s["words"]:
                out.append(Word(w[0], w[1], w[2], speaker=spk, conf=w[3] if len(w) > 3 else None))
    return out


def merge_speakers(transcript: dict, names: dict[str, str]) -> tuple[dict, dict[str, str]]:
    """Talare med samma namn slås ihop till den som hördes först; layouten byggs om."""
    by_name: dict[str, list[int]] = {}
    for k, v in names.items():
        by_name.setdefault(v.strip().lower(), []).append(int(k))
    remap = {i: min(ids) for ids in by_name.values() for i in ids}
    if all(k == v for k, v in remap.items()):
        return transcript, names
    words = words_from_transcript(transcript)
    for w in words:
        if w.speaker is not None:
            w.speaker = remap.get(w.speaker, w.speaker)
    merged = layout(words)
    kept = {str(min(ids)): names[str(min(ids))] for ids in by_name.values()}
    return merged, kept


def speaker_names(transcript: dict, names: dict | None = None) -> dict[str, str]:
    """Namn per talar-id. Tomt om bara en (eller ingen) talare hittades."""
    ids = sorted({p.get("speaker") for p in transcript.get("paragraphs", []) if p.get("speaker") is not None})
    if len(ids) < 2:
        return {}
    names = names or {}
    return {str(i): names.get(str(i)) or f"Talare {i + 1}" for i in ids}
