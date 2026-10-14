"""Stenograf – lokal webbapp: klistra in en videolänk, få en exakt rysk transkription."""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import urllib.parse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import downloader, exporters
from .asr import Word
from .textlayout import Sentence, cues, merge_speakers, retime, speaker_names
from .jobs import TERMINAL, Manager

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

app = FastAPI(title="Stenograf", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
mgr = Manager()

URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.I)
MODES = {"ru", "auto"}


class NewJob(BaseModel):
    url: str
    mode: str = "ru"
    speakers: bool = True
    num_speakers: int | None = None


def _num(n) -> int | None:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    return n if 2 <= n <= 12 else None


def _job_or_404(job_id: str):
    j = mgr.get(job_id)
    if not j:
        raise HTTPException(404, "Jobbet finns inte.")
    return j


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((STATIC / "index.html").read_text(), headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health():
    from .asr import pick_device

    return {"ok": True, "device": str(pick_device())}


@app.get("/api/jobs")
def list_jobs():
    return mgr.list()


@app.post("/api/jobs")
def create_job(body: NewJob):
    url = body.url.strip()
    if not URL_RE.match(url):
        raise HTTPException(400, "Det där ser inte ut som en länk. Den ska börja med https://")
    if body.mode not in MODES:
        raise HTTPException(400, "Okänt språkläge.")
    return mgr.submit_url(url, body.mode, body.speakers, _num(body.num_speakers)).public()


@app.post("/api/upload")
def upload(
    file: UploadFile = File(...),
    mode: str = Form("ru"),
    speakers: bool = Form(True),
    num_speakers: str = Form(""),
):
    if mode not in MODES:
        raise HTTPException(400, "Okänt språkläge.")
    name = pathlib.Path(file.filename or "fil").name
    return mgr.submit_file(name, file.file, mode, speakers, _num(num_speakers)).public()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    j = _job_or_404(job_id)
    out = j.public()
    if j.status == "done":
        doc = mgr.transcript(job_id) or {}
        out["transcript"] = doc.get("transcript")
        if out["transcript"]:
            out["speaker_names"] = speaker_names(out["transcript"], doc.get("speakers"))
    return out


@app.put("/api/jobs/{job_id}/speakers")
async def rename_speakers(job_id: str, request: Request):
    """{"names": {"0": "Ира", "1": "Даша"}}"""
    j = _job_or_404(job_id)
    doc = mgr.transcript(job_id)
    if not doc:
        raise HTTPException(409, "Transkriptionen är inte klar.")
    names = (await request.json()).get("names", {})
    cur = speaker_names(doc["transcript"], doc.get("speakers"))
    for k, v in names.items():
        v = " ".join(str(v).split())[:40]
        if k in cur and v:
            cur[k] = v
    # Samma namn på två talare = samma person: slå ihop och bygg om styckena.
    before = len(cur)
    merged, cur = merge_speakers(doc["transcript"], cur)
    doc["transcript"] = merged
    doc["speakers"] = cur
    mgr.save_transcript(j, doc)
    names = speaker_names(merged, cur)
    return {
        "ok": True,
        "speaker_names": names,
        "merged": len(cur) < before,
        "transcript": merged,
    }


@app.put("/api/jobs/{job_id}/transcript")
async def edit_transcript(job_id: str, request: Request):
    """Spara manuella texträttelser: {"edits": {"p.s": "ny text", ...}}."""
    j = _job_or_404(job_id)
    doc = mgr.transcript(job_id)
    if not doc:
        raise HTTPException(409, "Transkriptionen är inte klar.")
    edits = (await request.json()).get("edits", {})
    paras = doc["transcript"]["paragraphs"]
    for key, text in edits.items():
        try:
            pi, si = (int(x) for x in key.split("."))
            sent = paras[pi]["sentences"][si]
        except (ValueError, IndexError):
            raise HTTPException(400, f"Ogiltig mening: {key}")
        text = " ".join(str(text).split())
        if not text:
            continue
        sent["text"] = text
        sent["edited"] = True
        sent["words"] = retime(sent, text)
    sents = [
        Sentence(s["start"], s["end"], s["text"], [Word(w[0], w[1], w[2]) for w in s["words"]], s.get("speaker"))
        for p in paras
        for s in p["sentences"]
    ]
    doc["transcript"]["cues"] = [c.__dict__ for c in cues(sents)]
    mgr.save_transcript(j, doc)
    return {"ok": True, "transcript": doc["transcript"]}


@app.get("/api/jobs/{job_id}/events")
async def events(job_id: str, request: Request):
    j = _job_or_404(job_id)

    async def stream():
        idx = 0
        yield f"event: hello\ndata: {json.dumps(j.public(), ensure_ascii=False)}\n\n"
        while True:
            if await request.is_disconnected():
                return
            evs = await asyncio.to_thread(mgr.wait_events, j, idx, 10.0)
            if not evs:
                yield ": ping\n\n"
                if j.status in TERMINAL:
                    return
                continue
            idx += len(evs)
            for ev in evs:
                yield f"event: {ev['kind']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if j.status in TERMINAL and evs[-1]["kind"] == "status":
                return

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str):
    _job_or_404(job_id)
    return {"ok": mgr.cancel(job_id)}


class Retry(BaseModel):
    cookies: str | None = None


@app.get("/api/browsers")
def browsers():
    return downloader.installed_browsers()


@app.post("/api/jobs/{job_id}/retry")
def retry(job_id: str, body: Retry | None = None):
    _job_or_404(job_id)
    cookies = body.cookies if body else None
    if cookies and cookies not in downloader.BROWSERS:
        raise HTTPException(400, "Okänd webbläsare.")
    j = mgr.retry(job_id, cookies)
    if not j:
        raise HTTPException(409, "Jobbet kan inte startas om.")
    return j.public()


@app.delete("/api/jobs/{job_id}")
def delete(job_id: str):
    _job_or_404(job_id)
    return {"ok": mgr.delete(job_id)}


def _filename(title: str, ext: str) -> str:
    safe = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", title).strip()[:120] or "transkription"
    return safe + "." + ext


@app.get("/api/jobs/{job_id}/export/{fmt}")
def export(job_id: str, fmt: str):
    j = _job_or_404(job_id)
    if fmt not in exporters.FORMATS:
        raise HTTPException(404, "Okänt format.")
    doc = mgr.transcript(job_id)
    if not doc:
        raise HTTPException(409, "Transkriptionen är inte klar.")
    doc["meta"] = j.meta
    mime, fn = exporters.FORMATS[fmt]
    body = fn(doc)
    name = _filename(j.meta.get("title") or "transkription", fmt)
    disp = "attachment; filename*=UTF-8''" + urllib.parse.quote(name)
    return Response(body, media_type=mime, headers={"Content-Disposition": disp})


@app.exception_handler(HTTPException)
async def http_error(_, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
