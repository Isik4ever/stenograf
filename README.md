# Стенограф · Stenograf

Klistra in en länk till en video → få en exakt rysk transkription med tid för varje ord,
talare, sökning, redigering och export. Allt körs lokalt på Macen. Gratis, ingen API-nyckel,
inget lämnar datorn.

## Starta

Dubbelklicka på **Stenograf.app** (i den här mappen). Appen öppnas på http://127.0.0.1:8790.

Från terminalen:

```bash
./start.sh
```

Stoppa servern:

```bash
./stop.sh
```

## Vad som händer med en länk

1. **yt-dlp** hämtar ljudet: YouTube, VK Video, RuTube, Одноклассники, Дзен, Telegram, Vimeo och
   tusentals andra sajter. Egna ljud-/videofiler går att dra in i fönstret.
2. **Silero VAD** hittar talet och delar upp det i bitar på högst 22 s.
3. **GigaAM-v3 e2e (Sber, MIT)** transkriberar på Apple-GPU:n, med skiljetecken och tid per ord.
   Klart bäst på ryska: ca 8 % WER mot Whisper large-v3:s ca 25 % i Sbers jämförelse.
   ~1 timme video ≈ 1–2 minuter på M4 Pro.
4. **sherpa-onnx** (pyannote-segmentering + TitaNet-röstavtryck) skiljer talare, parallellt på CPU:n.
5. Texten delas i meningar, stycken (ny talare = nytt stycke) och undertextrutor (max 2 × 42 tecken, 7 s).

Läget **Annat språk** använder Whisper large-v3-turbo och upptäcker språket automatiskt.

## I läsvyn

- Klicka på ett ord eller en tid → videon spelas därifrån (inbäddad från källan). Texten "bläckas" i takt.
- `Mellanslag` spela/pausa · `←` `→` 5 s · `⌘F` sök (fraser fungerar, ё = е).
- **Redigera**: rätta meningar direkt i texten. Undertexterna räknas om.
- Klicka på en talaretikett för att döpa om talaren (t.ex. "Ира").
- **Exportera**: TXT, TXT med tider, Word (.docx), SRT, VTT, Markdown, JSON (ord + tider).

## Om YouTube kräver inloggning

Vissa videor (åldersgräns, botkontroll) kräver inloggning. Felrutan låter dig då välja en
webbläsare att låna inloggningen från, bara för den hämtningen. macOS kan fråga om lov.
Tillfälliga 403-fel från YouTube hanteras automatiskt med nya försök.

## Data och integritet

- Under jobbet laddas bara ljudspåret ner till `data/jobs/<id>/`. Filen raderas direkt när ljudet
  avkodats till minnet (några sekunder), och allt utom texten städas bort när jobbet är klart,
  avbrutet eller misslyckat.
- Kvar blir bara `job.json` (titel, länk, tider) och `transcript.json` (texten med ordtider).
- Uppspelning sker från källan: YouTube (via youtube-nocookie), VK Video och RuTube bäddas in.
  Vägrar källan inbäddning öppnar en klick på en tid videon där i stället.
- Modeller: `~/.cache/gigaam` (~430 MB), `~/.cache/stenograf` (~46 MB), Whisper i `~/.cache/huggingface`.

## Utveckling

```bash
.venv/bin/python -m pytest
```

Kod: `app/asr.py` (VAD + motorer), `app/diarize.py` (talare), `app/textlayout.py` (meningar,
stycken, undertexter), `app/jobs.py` (kö + händelser), `app/server.py` (FastAPI), `static/` (UI).
