"""Jobbkö: en arbetstråd (GPU:n delas inte), händelseström per jobb, allt sparat på disk."""

from __future__ import annotations

import json
import pathlib
import queue
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field

from . import asr, downloader, textlayout

DATA = pathlib.Path(__file__).resolve().parent.parent / "data" / "jobs"

TERMINAL = {"done", "error", "cancelled"}

# Det enda som får ligga kvar i ett jobb när det är klart: texten och dess metadata.
KEEP = {"job.json", "transcript.json"}


def purge_media(job_dir: pathlib.Path) -> int:
    """Radera allt nedladdat/uppladdat material (video, ljud, delfiler, bilder). Returnerar antal bytes."""
    freed = 0
    if not job_dir.is_dir():
        return 0
    for f in job_dir.iterdir():
        if f.name in KEEP:
            continue
        try:
            if f.is_dir():
                freed += sum(x.stat().st_size for x in f.rglob("*") if x.is_file())
                shutil.rmtree(f, ignore_errors=True)
            else:
                freed += f.stat().st_size
                f.unlink()
        except FileNotFoundError:
            pass
    return freed

# Hur stor del av totalprogressen varje steg motsvarar (för en jämn stapel).
STAGES = {
    "queued": (0.0, 0.0),
    "probing": (0.0, 0.02),
    "downloading": (0.02, 0.30),
    "preparing": (0.30, 0.34),
    "vad": (0.34, 0.40),
    "loading_model": (0.40, 0.42),
    "transcribing": (0.42, 0.95),
    "speakers": (0.95, 0.99),
    "finishing": (0.99, 1.0),
    "done": (1.0, 1.0),
}

STAGE_LABEL = {
    "queued": "I kö",
    "probing": "Läser länken",
    "downloading": "Hämtar ljud",
    "preparing": "Förbereder ljud",
    "vad": "Hittar tal",
    "loading_model": "Laddar modell",
    "transcribing": "Transkriberar",
    "speakers": "Skiljer talare",
    "finishing": "Formaterar text",
    "done": "Klar",
    "error": "Fel",
    "cancelled": "Avbruten",
}


@dataclass
class Job:
    id: str
    source: str  # "url" | "file"
    url: str | None
    mode: str  # "ru" | "auto"
    created: float
    status: str = "queued"
    progress: float = 0.0
    detail: str = ""
    error: str | None = None
    meta: dict = field(default_factory=dict)
    engine: str | None = None
    language: str | None = None
    timings: dict = field(default_factory=dict)
    upload_name: str | None = None
    speakers: bool = True
    num_speakers: int | None = None  # None = räkna ut själv
    cookies: str | None = None  # webbläsare att låna inloggning från, bara om användaren valt det
    error_kind: str | None = None

    # körtid, sparas inte
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    events: list = field(default_factory=list, repr=False)
    cond: threading.Condition = field(default_factory=threading.Condition, repr=False)

    @property
    def dir(self) -> pathlib.Path:
        return DATA / self.id

    def public(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "url": self.url,
            "mode": self.mode,
            "speakers": self.speakers,
            "num_speakers": self.num_speakers,
            "created": self.created,
            "status": self.status,
            "stage_label": STAGE_LABEL.get(self.status, self.status),
            "progress": round(self.progress, 4),
            "detail": self.detail,
            "error": self.error,
            "error_kind": self.error_kind,
            "cookies": self.cookies,
            "meta": self.meta,
            "engine": self.engine,
            "language": self.language,
            "timings": self.timings,
        }

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "job.json.tmp"
        tmp.write_text(json.dumps(self.public() | {"upload_name": self.upload_name}, ensure_ascii=False, indent=1))
        tmp.replace(self.dir / "job.json")


class Manager:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.q: queue.Queue[str] = queue.Queue()
        self.lock = threading.Lock()
        self._load()
        threading.Thread(target=self._worker, daemon=True, name="stenograf-worker").start()

    # ------------------------------------------------------------ lagring

    def _load(self) -> None:
        for f in DATA.glob("*/job.json"):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            j = Job(
                id=d["id"], source=d.get("source", "url"), url=d.get("url"), mode=d.get("mode", "ru"),
                created=d.get("created", f.stat().st_mtime), status=d.get("status", "error"),
                progress=d.get("progress", 0.0), detail=d.get("detail", ""), error=d.get("error"),
                meta=d.get("meta", {}), engine=d.get("engine"), language=d.get("language"),
                timings=d.get("timings", {}), upload_name=d.get("upload_name"),
                cookies=d.get("cookies"), error_kind=d.get("error_kind"), speakers=d.get("speakers", True),
                num_speakers=d.get("num_speakers"),
            )
            if j.status not in TERMINAL:
                j.status, j.error = "error", "Avbröts eftersom appen stängdes. Starta om jobbet."
                j.save()
            # Städa även jobb från äldre versioner som sparade ljud och video.
            purge_media(j.dir)
            self.jobs[j.id] = j

    def list(self) -> list[dict]:
        with self.lock:
            js = sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)
        return [j.public() for j in js]

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def transcript(self, job_id: str) -> dict | None:
        f = DATA / job_id / "transcript.json"
        if not f.exists():
            return None
        return json.loads(f.read_text())

    def save_transcript(self, job: Job, doc: dict) -> None:
        tmp = job.dir / "transcript.json.tmp"
        tmp.write_text(json.dumps(doc, ensure_ascii=False))
        tmp.replace(job.dir / "transcript.json")

    # ------------------------------------------------------------ åtgärder

    def submit_url(self, url: str, mode: str, speakers: bool = True, num_speakers: int | None = None) -> Job:
        j = Job(
            id=uuid.uuid4().hex[:12], source="url", url=url.strip(), mode=mode, created=time.time(),
            speakers=speakers, num_speakers=num_speakers,
        )
        j.meta = {"title": url.strip(), "url": url.strip()}
        return self._enqueue(j)

    def submit_file(
        self, filename: str, fileobj, mode: str, speakers: bool = True, num_speakers: int | None = None
    ) -> Job:
        j = Job(
            id=uuid.uuid4().hex[:12], source="file", url=None, mode=mode, created=time.time(),
            upload_name=filename, speakers=speakers, num_speakers=num_speakers,
        )
        j.dir.mkdir(parents=True, exist_ok=True)
        ext = pathlib.Path(filename).suffix.lower()[:8] or ".bin"
        with open(j.dir / f"source{ext}", "wb") as out:
            shutil.copyfileobj(fileobj, out, length=4 * 1024 * 1024)
        j.meta = {"title": pathlib.Path(filename).stem, "uploader": "", "url": None, "extractor": "Fil"}
        return self._enqueue(j)

    def _enqueue(self, j: Job) -> Job:
        with self.lock:
            self.jobs[j.id] = j
        j.save()
        self._emit(j, "status")
        self.q.put(j.id)
        return j

    def cancel(self, job_id: str) -> bool:
        j = self.jobs.get(job_id)
        if not j or j.status in TERMINAL:
            return False
        j.cancel.set()
        if j.status == "queued":
            self._finish(j, "cancelled")
        return True

    def retry(self, job_id: str, cookies: str | None = None) -> Job | None:
        j = self.jobs.get(job_id)
        if not j or j.status not in TERMINAL - {"done"}:
            return None
        j.cancel = threading.Event()
        if cookies is not None:
            j.cookies = cookies or None
        j.status, j.error, j.error_kind, j.progress, j.detail = "queued", None, None, 0.0, ""
        j.events = []
        j.save()
        self._emit(j, "status")
        self.q.put(j.id)
        return j

    def delete(self, job_id: str) -> bool:
        j = self.jobs.get(job_id)
        if not j:
            return False
        j.cancel.set()
        with self.lock:
            self.jobs.pop(job_id, None)
        shutil.rmtree(j.dir, ignore_errors=True)
        return True

    # ------------------------------------------------------------ händelser

    def _emit(self, j: Job, kind: str, data: dict | None = None) -> None:
        ev = {"kind": kind, "job": j.public()}
        if data:
            ev.update(data)
        with j.cond:
            j.events.append(ev)
            j.cond.notify_all()

    def wait_events(self, j: Job, since: int, timeout: float = 15.0) -> list:
        with j.cond:
            if len(j.events) <= since:
                j.cond.wait(timeout)
            return j.events[since:]

    def _stage(self, j: Job, status: str, frac: float = 0.0, detail: str = "") -> None:
        if j.cancel.is_set():
            raise downloader.Cancelled()
        lo, hi = STAGES[status]
        prog = lo + (hi - lo) * max(0.0, min(1.0, frac))
        now = time.monotonic()
        # Strypning: nedladdningskroken anropas hundratals gånger per sekund.
        same = status == j.status and detail == j.detail
        if same and now - getattr(j, "_last_emit", 0.0) < 0.2 and prog - j.progress < 0.01:
            return
        j._last_emit = now
        j.status, j.progress, j.detail = status, prog, detail
        self._emit(j, "progress")

    def _finish(self, j: Job, status: str, error: str | None = None, kind: str | None = None) -> None:
        if status in TERMINAL:
            purge_media(j.dir)
        j.status = status
        j.error = error
        j.error_kind = kind
        if status == "done":
            j.progress = 1.0
        j.detail = ""
        j.save()
        self._emit(j, "status")

    # ------------------------------------------------------------ arbetaren

    def _worker(self) -> None:
        while True:
            job_id = self.q.get()
            j = self.jobs.get(job_id)
            if not j or j.status != "queued":
                continue
            if j.cancel.is_set():
                self._finish(j, "cancelled")
                continue
            try:
                self._run(j)
            except downloader.Cancelled:
                self._finish(j, "cancelled")
            except downloader.DownloadError as e:
                self._finish(j, "error", str(e), e.kind)
            except Exception as e:
                traceback.print_exc()
                self._finish(j, "error", f"Oväntat fel: {e}")

    def _run(self, j: Job) -> None:
        t0 = time.time()
        j.dir.mkdir(parents=True, exist_ok=True)

        # 1. Källa
        if j.source == "url":
            self._stage(j, "probing", 0.5)
            meta = downloader.probe(j.url, j.cookies)
            if meta.get("is_live"):
                raise downloader.DownloadError("Livesändningar kan inte transkriberas förrän de är avslutade.")
            j.meta = meta
            j.save()
            self._emit(j, "meta")
            self._stage(j, "downloading", 0.0, "Startar …")
            for old in j.dir.glob("source.*"):
                old.unlink()
            src = downloader.download_audio(
                j.url, j.dir, lambda f, label: self._stage(j, "downloading", f, label), j.cancel.is_set, j.cookies
            )
        else:
            cands = [p for p in j.dir.glob("source.*")]
            if not cands:
                raise downloader.DownloadError("Filen är redan raderad. Ladda upp den igen för ett nytt försök.")
            src = cands[0]
        j.timings["download"] = round(time.time() - t0, 1)

        # 2. Ljud
        self._stage(j, "preparing", 0.1, "Avkodar")
        wav = asr.load_wav(str(src))
        duration = wav.shape[-1] / asr.SR
        if duration < 0.5:
            raise downloader.DownloadError("Filen innehåller inget ljud.")
        j.meta["duration"] = j.meta.get("duration") or round(duration, 1)
        # Ljudet finns nu i minnet; filen på disken behövs inte längre.
        purge_media(j.dir)
        j.save()
        self._emit(j, "meta")

        # 3. Talare (CPU) parallellt med taligenkänningen (GPU)
        dia: dict = {}
        dia_thread = None
        if j.speakers:
            from . import diarize

            def run_dia():
                t = time.time()
                try:
                    dia["segs"] = diarize.diarize(
                        wav, lambda f: dia.__setitem__("progress", f), j.num_speakers
                    )
                except Exception as e:  # talare är en bonus, aldrig ett stopp
                    traceback.print_exc()
                    dia["error"] = str(e)
                dia["time"] = round(time.time() - t, 1)

            dia_thread = threading.Thread(target=run_dia, daemon=True, name=f"diarize-{j.id}")
            dia_thread.start()

        # 4. Motor
        engine_key = "gigaam" if j.mode == "ru" else "whisper"
        t1 = time.time()
        results: list[asr.ChunkResult] = []

        def partial(r: asr.ChunkResult):
            self._emit(j, "partial", {"chunk": {"start": r.start, "end": r.end, "text": r.text}})

        if engine_key == "gigaam":
            self._stage(j, "vad", 0.0)
            chunks = asr.speech_chunks(wav, lambda p: self._stage(j, "vad", p))
            if not chunks:
                raise downloader.DownloadError("Hittade inget tal i ljudet.")
            self._stage(j, "loading_model", 0.5, asr.GigaAMEngine.label)
            eng = asr.get_engine("gigaam")
            j.engine, j.language = eng.label, "ru"
            speech_s = sum(c.end - c.start for c in chunks) / asr.SR
            self._stage(j, "transcribing", 0.0, f"{len(chunks)} talsegment · {speech_s / 60:.1f} min tal")

            def on_chunk(r, done, total):
                partial(r)
                self._stage(j, "transcribing", done / total, f"{done} / {total} segment")

            results = eng.run(wav, chunks, on_chunk=on_chunk)
            self._stage(j, "transcribing", 1.0, "Kontrollerar meningsgränser")
            j.timings["boundaries_fixed"] = asr.fix_boundaries(eng, wav, results)
        else:
            self._stage(j, "loading_model", 0.5, asr.WhisperEngine.label)
            eng = asr.get_engine("whisper")
            j.engine = eng.label
            self._stage(j, "transcribing", 0.0, "Upptäcker språk …")

            def on_seg(r, frac):
                partial(r)
                self._stage(j, "transcribing", frac, f"{frac * 100:.0f} %")

            results, lang = eng.run_full(wav, None, on_chunk=on_seg)
            j.language = lang
        j.timings["transcribe"] = round(time.time() - t1, 1)

        words = list(asr.iter_words(results))
        if not words:
            raise downloader.DownloadError("Hittade inget tal i ljudet.")

        if dia_thread is not None:
            while dia_thread.is_alive():
                self._stage(j, "speakers", dia.get("progress", 0.0), "Röstavtryck per talsegment")
                dia_thread.join(0.4)
            j.timings["speakers"] = dia.get("time")
            if dia.get("segs"):
                from . import diarize

                for w, spk in zip(words, diarize.assign(words, dia["segs"])):
                    w.speaker = spk

        # 5. Layout + spara
        self._stage(j, "finishing", 0.5)
        transcript = textlayout.layout(words)
        doc = {
            "version": 1,
            "meta": j.meta,
            "engine": j.engine,
            "language": j.language,
            "speakers": textlayout.speaker_names(transcript),
            "transcript": transcript,
        }
        self.save_transcript(j, doc)
        j.timings["total"] = round(time.time() - t0, 1)
        self._finish(j, "done")
