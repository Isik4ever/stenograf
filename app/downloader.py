"""Hämta ljud från en videolänk med yt-dlp (YouTube, VK Video, RuTube, OK, Dzen, Telegram m.fl.)."""

from __future__ import annotations

import os
import shutil
import pathlib
from typing import Callable

import yt_dlp


class DownloadError(Exception):
    """Fel som går att visa direkt för användaren. kind="auth" = källan vill ha inloggning."""

    def __init__(self, message: str, kind: str | None = None):
        super().__init__(message)
        self.kind = kind


# Webbläsare som yt-dlp kan läsa cookies från, med appnamnet i /Applications.
BROWSERS = {
    "chrome": ("Google Chrome", "Google Chrome.app"),
    "safari": ("Safari", "Safari.app"),
    "firefox": ("Firefox", "Firefox.app"),
    "edge": ("Microsoft Edge", "Microsoft Edge.app"),
    "brave": ("Brave", "Brave Browser.app"),
    "opera": ("Opera", "Opera.app"),
    "vivaldi": ("Vivaldi", "Vivaldi.app"),
    "chromium": ("Chromium", "Chromium.app"),
}


def installed_browsers() -> list[dict]:
    roots = [pathlib.Path("/Applications"), pathlib.Path("/System/Applications"), pathlib.Path.home() / "Applications"]
    out = []
    for key, (label, app) in BROWSERS.items():
        if any((r / app).exists() for r in roots) or (key == "safari" and pathlib.Path("/System/Cryptexes/App/System/Applications/Safari.app").exists()):
            out.append({"key": key, "label": label})
    return out


class Cancelled(Exception):
    pass


def _js_runtimes() -> dict:
    """node/deno även när appen startas utan nvm i PATH (t.ex. från Stenograf.app)."""
    found: dict = {}
    for name in ("deno", "node"):
        path = shutil.which(name)
        if not path and name == "node":
            cands = sorted(pathlib.Path.home().glob(".nvm/versions/node/*/bin/node"))
            path = str(cands[-1]) if cands else None
        if path:
            found[name] = {"path": path}
    return found or {"node": {}}


def _opts(extra: dict | None = None, cookies: str | None = None) -> dict:
    o = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        # YouTube kräver JS-runtime för signaturer sedan 2025; node finns via nvm,
        # lösarskripten kommer från paketet yt-dlp-ejs (ingen fjärrkod).
        "js_runtimes": _js_runtimes(),
    }
    browser = cookies or os.environ.get("STENOGRAF_COOKIES_BROWSER")
    if browser:
        o["cookiesfrombrowser"] = (browser,)
    if extra:
        o.update(extra)
    return o


def _friendly(err: Exception) -> DownloadError:
    msg = str(err)
    low = msg.lower()
    if "could not find" in low and "cookies" in low or "failed to decrypt" in low or "operation not permitted" in low:
        return DownloadError(
            "Kunde inte läsa inloggningen från webbläsaren. Är du inloggad där, och tillät du åtkomst i macOS-rutan?",
            "auth",
        )
    if "unsupported url" in low:
        return DownloadError("Länken stöds inte. Klistra in en direktlänk till en video (YouTube, VK, RuTube, OK, Dzen …).")
    if "private" in low:
        return DownloadError("Videon är privat och går inte att hämta.")
    if "sign in" in low or "login" in low or "cookies" in low or "confirm you" in low or "age" in low and "restrict" in low:
        return DownloadError(
            "Källan vill att du är inloggad för den här videon (t.ex. åldersgräns eller botkontroll).",
            "auth",
        )
    if "geo" in low or "not available in your country" in low:
        return DownloadError("Videon är regionlåst i ditt land.")
    if "429" in low or "too many requests" in low:
        return DownloadError("Källan begränsar antalet förfrågningar just nu. Försök igen om en stund.")
    if "is not a valid url" in low or "no video formats" in low:
        return DownloadError("Hittade ingen video på länken.")
    first = msg.replace("ERROR: ", "").strip().splitlines()[0] if msg.strip() else type(err).__name__
    return DownloadError(f"Kunde inte hämta videon: {first[:300]}")


def probe(url: str, cookies: str | None = None) -> dict:
    """Metadata utan nedladdning."""
    try:
        with yt_dlp.YoutubeDL(_opts({"skip_download": True}, cookies)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # yt-dlp kastar många olika typer
        raise _friendly(e) from e
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise DownloadError("Länken är en spellista utan videor.")
        info = entries[0]
    return {
        "title": info.get("title") or "Utan titel",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
        "video_id": info.get("id"),
        "language": info.get("language"),
        "upload_date": info.get("upload_date"),
        "is_live": bool(info.get("is_live")),
    }


def _transient(err: Exception) -> bool:
    low = str(err).lower()
    return any(
        k in low
        for k in ("403", "forbidden", "unable to download video data", "timed out", "http error 5",
                  "requested format is not available", "connection reset", "incomplete")
    )


def download_audio(
    url: str,
    dest_dir: pathlib.Path,
    on_progress: Callable[[float, str], None],
    should_stop: Callable[[], bool],
    cookies: str | None = None,
) -> pathlib.Path:
    """Laddar ner bästa ljudspåret (eller hela videon om ljud saknas separat). Returnerar filsökvägen."""

    def hook(d):
        if should_stop():
            raise Cancelled()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            frac = done / total if total else 0.0
            speed = d.get("speed") or 0
            label = f"{done / 1e6:.1f} / {total / 1e6:.1f} MB" if total else f"{done / 1e6:.1f} MB"
            if speed:
                label += f" · {speed / 1e6:.1f} MB/s"
            on_progress(min(frac, 1.0), label)

    # YouTube ger ibland 403 på enstaka format eller klienter. Nytt försök hämtar färska
    # länkar; sista försöket tar ett annat klientval och tillåter kombinerade format.
    attempts = [
        {},
        {"extractor_args": {"youtube": {"player_client": ["default", "-web"]}}},
        {"format": "bestaudio*/best*", "extractor_args": {"youtube": {"player_client": ["default", "-web_safari"]}}},
    ]
    last: Exception | None = None
    path = None
    for n, variant in enumerate(attempts):
        for old in dest_dir.glob("source.*"):
            old.unlink(missing_ok=True)
        opts = _opts(
            {
                "format": "bestaudio/best",
                "outtmpl": str(dest_dir / "source.%(ext)s"),
                "progress_hooks": [hook],
                **variant,
            },
            cookies,
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = pathlib.Path(ydl.prepare_filename(info))
            break
        except Cancelled:
            raise
        except yt_dlp.utils.DownloadError as e:
            if isinstance(e.exc_info[1] if e.exc_info else None, Cancelled):
                raise Cancelled() from e
            last = e
            if not _transient(e) or n == len(attempts) - 1:
                raise _friendly(e) from e
            on_progress(0.0, f"Nytt försök ({n + 2}/{len(attempts)}) …")
        except Exception as e:
            raise _friendly(e) from e
    if path is None:
        raise _friendly(last or Exception("okänt fel"))
    if not path.exists():
        cands = sorted(dest_dir.glob("source.*"))
        if not cands:
            raise DownloadError("Nedladdningen gav ingen fil.")
        path = cands[0]
    return path
