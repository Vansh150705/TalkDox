"""YouTube ingestion with an explicit, ordered fallback chain and honest failure reporting.

Transcript providers, tried in order until one returns real caption text:
  1. youtube-transcript-api   (caption tracks: manual preferred over auto-generated, any language)
  2. Supadata API             (if SUPADATA_API_KEY set; also works when YouTube blocks datacenter IPs)
  3. yt-dlp subtitle tracks   (if installed and ENABLE_YTDLP_FALLBACK)
  4. Gemini video understanding of the public URL (opt-in ENABLE_GEMINI_VIDEO_FALLBACK; output is
     MODEL-GENERATED, clearly labelled, and never presented as a verbatim caption)
If everything fails the ingest fails with a specific reason. Title/description metadata is NEVER used as
a substitute for video content. Only the spoken-word transcript is indexed; no claim is made about
visuals (`modality` = "transcript").
"""
from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

import httpx

from ..config import Settings
from ..errors import AppError, IngestError
from ..net import SafeFetcher
from .models import Block, Extracted

log = logging.getLogger("talkdox.youtube")

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com",
          "www.youtube-nocookie.com", "youtu.be", "www.youtu.be"}


@dataclass
class YTRef:
    kind: str                 # video | playlist
    video_id: Optional[str] = None
    playlist_id: Optional[str] = None
    start: int = 0


def parse_youtube_url(url: str) -> YTRef:
    """Strict parser: the host must really be YouTube (the previous regex accepted any URL containing 'v=')."""
    raw = (url or "").strip()
    if _ID.match(raw):
        return YTRef("video", raw)
    if "://" not in raw:
        raw = "https://" + raw
    try:
        p = urlsplit(raw)
    except ValueError:
        raise IngestError("That doesn't look like a valid YouTube link.", code="invalid_url")
    host = (p.hostname or "").lower()
    if host not in _HOSTS or p.scheme not in ("http", "https"):
        raise IngestError("That doesn't look like a YouTube link (youtube.com or youtu.be).", code="invalid_url")
    q = parse_qs(p.query)
    start = 0
    t = (q.get("t") or q.get("start") or ["0"])[0]
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", t)
    if m and any(m.groups()):
        start = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)
    vid = None
    parts = [x for x in p.path.split("/") if x]
    if host.endswith("youtu.be"):
        vid = parts[0] if parts else None
    elif parts and parts[0] in ("shorts", "embed", "live", "v", "e") and len(parts) > 1:
        vid = parts[1]
    elif parts and parts[0] == "watch":
        vid = (q.get("v") or [None])[0]
    if vid and _ID.match(vid):
        return YTRef("video", vid, (q.get("list") or [None])[0], start)
    pl = (q.get("list") or [None])[0]
    if pl and re.fullmatch(r"[A-Za-z0-9_-]{10,60}", pl):
        return YTRef("playlist", None, pl)
    raise IngestError("Couldn't find a video ID in that link. Paste the full YouTube video URL.", code="invalid_url")


# ----------------------------------------------------------------------------- data classes
@dataclass
class Seg:
    text: str
    start: float
    dur: float = 0.0


@dataclass
class Transcript:
    segments: list[Seg]
    language: str = ""
    kind: str = "unknown"           # manual | generated | unknown | model_generated
    provider: str = ""
    available_languages: list[str] = field(default_factory=list)


@dataclass
class VideoMeta:
    video_id: str
    title: str = ""
    author: str = ""
    description: str = ""
    duration: Optional[int] = None
    is_live: bool = False
    chapters: list[tuple[float, str]] = field(default_factory=list)


class TranscriptFailure(Exception):
    def __init__(self, code: str, message: str, terminal: bool = False):
        super().__init__(message)
        self.code, self.message, self.terminal = code, message, terminal


FAIL_MESSAGES = {
    "captions_disabled": "The uploader has disabled captions for this video, so there is no transcript to read.",
    "no_transcript": "No caption track is available for this video.",
    "video_unavailable": "This video is unavailable (private, removed or region-locked).",
    "age_restricted": "This video is age-restricted; its captions can't be retrieved without signing in.",
    "blocked_by_youtube": "YouTube is blocking transcript requests from this server's network. Configure "
                          "SUPADATA_API_KEY or YOUTUBE_PROXY_URL (see docs/DEPLOYMENT.md) or try again later.",
    "live_stream": "Live streams and upcoming premieres can't be processed until they have ended.",
}


# ----------------------------------------------------------------------------- providers
class TranscriptApiProvider:
    name = "youtube-transcript-api"

    def __init__(self, s: Settings):
        self.s = s

    def fetch(self, video_id: str, langs: list[str]) -> Transcript:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api import _errors as E
        proxy = None
        if self.s.youtube_proxy_url:
            from youtube_transcript_api.proxies import GenericProxyConfig
            proxy = GenericProxyConfig(http_url=self.s.youtube_proxy_url, https_url=self.s.youtube_proxy_url)
        api = YouTubeTranscriptApi(proxy_config=proxy)
        try:
            tl = api.list(video_id)
            tracks = list(tl)
            avail = [t.language_code for t in tracks]
            chosen = None
            for want_generated in (False, True):          # manual beats auto-generated
                for lang in langs:
                    for t in tracks:
                        if t.is_generated == want_generated and (t.language_code == lang or t.language_code.startswith(lang + "-")):
                            chosen = t
                            break
                    if chosen:
                        break
                if chosen:
                    break
            if not chosen:                                  # fall back to whatever exists, manual first
                chosen = next((t for t in tracks if not t.is_generated), None) or (tracks[0] if tracks else None)
            if not chosen:
                raise TranscriptFailure("no_transcript", FAIL_MESSAGES["no_transcript"], terminal=True)
            fetched = chosen.fetch()
            segs = [Seg(_clean(sn.text), float(sn.start), float(sn.duration)) for sn in fetched]
            segs = [x for x in segs if x.text]
            return Transcript(segs, chosen.language_code, "generated" if chosen.is_generated else "manual",
                              self.name, avail)
        except E.TranscriptsDisabled:
            raise TranscriptFailure("captions_disabled", FAIL_MESSAGES["captions_disabled"], terminal=True)
        except E.NoTranscriptFound:
            raise TranscriptFailure("no_transcript", FAIL_MESSAGES["no_transcript"], terminal=True)
        except (E.VideoUnavailable, E.VideoUnplayable, E.InvalidVideoId):
            raise TranscriptFailure("video_unavailable", FAIL_MESSAGES["video_unavailable"], terminal=True)
        except E.AgeRestricted:
            raise TranscriptFailure("age_restricted", FAIL_MESSAGES["age_restricted"], terminal=True)
        except (E.RequestBlocked, E.IpBlocked):
            raise TranscriptFailure("blocked_by_youtube", FAIL_MESSAGES["blocked_by_youtube"])
        except TranscriptFailure:
            raise
        except Exception as e:
            log.info("transcript-api failed: %s", type(e).__name__)
            raise TranscriptFailure("transcript_failed", "Transcript retrieval failed.")


class SupadataProvider:
    name = "supadata"

    def __init__(self, s: Settings):
        self.s = s

    def fetch(self, video_id: str, langs: list[str]) -> Transcript:
        if not self.s.supadata_api_key:
            raise TranscriptFailure("not_configured", "Supadata not configured.")
        params = {"videoId": video_id, "text": "false"}
        if langs:
            params["lang"] = langs[0]
        hdr = {"x-api-key": self.s.supadata_api_key}
        try:
            r = httpx.get("https://api.supadata.ai/v1/youtube/transcript", params=params, headers=hdr, timeout=45)
            if r.status_code == 202 or (r.status_code == 200 and "jobId" in r.json()):
                job = r.json().get("jobId")
                for _ in range(30):
                    time.sleep(2)
                    jr = httpx.get(f"https://api.supadata.ai/v1/transcript/{job}", headers=hdr, timeout=30)
                    if jr.status_code == 200 and jr.json().get("status") in (None, "completed"):
                        r = jr
                        break
                    if jr.status_code == 200 and jr.json().get("status") == "failed":
                        raise TranscriptFailure("transcript_failed", "Transcript retrieval failed.")
        except httpx.HTTPError:
            raise TranscriptFailure("transcript_failed", "Transcript retrieval failed.")
        if r.status_code != 200:
            raw = r.text.lower()
            if r.status_code == 404 or "not found" in raw and "transcript" not in raw:
                raise TranscriptFailure("video_unavailable", FAIL_MESSAGES["video_unavailable"], terminal=True)
            if "transcript-unavailable" in raw or "no transcript" in raw or "unavailable" in raw and "transcript" in raw:
                raise TranscriptFailure("no_transcript", FAIL_MESSAGES["no_transcript"], terminal=True)
            if r.status_code in (401, 403, 429):
                raise TranscriptFailure("transcript_failed", "Transcript service refused the request.")
            raise TranscriptFailure("transcript_failed", "Transcript retrieval failed.")
        data = r.json()
        content = data.get("content")
        if isinstance(content, str):                       # plain text carries no timestamps -> unusable for citations
            raise TranscriptFailure("transcript_failed", "Transcript service returned no timestamps.")
        segs = []
        for it in content or []:
            if "offset" in it:
                start = float(it["offset"]) / 1000.0
                dur = float(it.get("duration", 0)) / 1000.0
            else:
                start, dur = float(it.get("start", 0)), float(it.get("duration", 0))
            txt = _clean(it.get("text", ""))
            if txt:
                segs.append(Seg(txt, start, dur))
        if not segs:
            raise TranscriptFailure("no_transcript", FAIL_MESSAGES["no_transcript"], terminal=True)
        return Transcript(segs, data.get("lang", ""), "unknown", self.name, data.get("availableLangs", []))


class YtDlpProvider:
    name = "yt-dlp"

    def __init__(self, s: Settings, fetcher: SafeFetcher):
        self.s, self.f = s, fetcher

    def fetch(self, video_id: str, langs: list[str]) -> Transcript:
        try:
            import yt_dlp
        except ImportError:
            raise TranscriptFailure("not_configured", "yt-dlp not installed.")
        opts = {"skip_download": True, "quiet": True, "no_warnings": True, "socket_timeout": 20,
                "noplaylist": True, "extract_flat": False}
        if self.s.youtube_proxy_url:
            opts["proxy"] = self.s.youtube_proxy_url
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        except Exception as e:
            msg = str(e).lower()
            if "private" in msg or "unavailable" in msg or "removed" in msg:
                raise TranscriptFailure("video_unavailable", FAIL_MESSAGES["video_unavailable"], terminal=True)
            if "sign in" in msg and "age" in msg:
                raise TranscriptFailure("age_restricted", FAIL_MESSAGES["age_restricted"], terminal=True)
            raise TranscriptFailure("transcript_failed", "Transcript retrieval failed.")
        manual, auto = info.get("subtitles") or {}, info.get("automatic_captions") or {}
        pick = None
        for table, kind in ((manual, "manual"), (auto, "generated")):
            for lang in langs + list(table.keys()):
                for code in table:
                    if code == lang or code.startswith(lang + "-"):
                        pick = (code, table[code], kind)
                        break
                if pick:
                    break
            if pick:
                break
        if not pick:
            raise TranscriptFailure("no_transcript", FAIL_MESSAGES["no_transcript"], terminal=True)
        code, formats, kind = pick
        fmt = next((f for f in formats if f.get("ext") == "json3"), None)
        if not fmt:
            raise TranscriptFailure("no_transcript", FAIL_MESSAGES["no_transcript"])
        res = self.f.get(fmt["url"], max_bytes=10 * 1024 * 1024, accept="application/json,*/*")
        segs = parse_json3(res.text)
        if not segs:
            raise TranscriptFailure("no_transcript", FAIL_MESSAGES["no_transcript"])
        return Transcript(segs, code, kind, self.name, sorted(set(manual) | set(auto)))


def parse_json3(text: str) -> list[Seg]:
    import json
    try:
        data = json.loads(text)
    except ValueError:
        return []
    segs = []
    for ev in data.get("events", []):
        parts = ev.get("segs")
        if not parts:
            continue
        t = _clean("".join(p.get("utf8", "") for p in parts))
        if t:
            segs.append(Seg(t, ev.get("tStartMs", 0) / 1000.0, ev.get("dDurationMs", 0) / 1000.0))
    return segs


class GeminiVideoProvider:
    """Opt-in last resort: asks Gemini to watch the public video and write a timestamped transcript.
    The result is model-generated (may contain errors/omissions) and is labelled as such everywhere."""
    name = "gemini-video"

    def __init__(self, s: Settings):
        self.s = s

    def fetch(self, video_id: str, langs: list[str]) -> Transcript:
        if not (self.s.enable_gemini_video_fallback and self.s.gemini_api_key):
            raise TranscriptFailure("not_configured", "Video understanding fallback not enabled.")
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=self.s.gemini_api_key,
                              http_options=types.HttpOptions(timeout=int(max(self.s.llm_timeout_s, 300) * 1000)))
        prompt = ("Transcribe the spoken audio of this video as faithfully as possible. Output one line per "
                  "utterance in the exact format `[MM:SS] text` (or `[H:MM:SS] text`). Do not describe visuals, "
                  "do not summarise, do not add commentary. If there is no speech, output NO_SPEECH.")
        try:
            resp = client.models.generate_content(
                model=self.s.gemini_model,
                contents=types.Content(parts=[types.Part(file_data=types.FileData(
                    file_uri=f"https://www.youtube.com/watch?v={video_id}")), types.Part(text=prompt)]),
                config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=60000))
        except Exception as e:
            log.info("gemini video fallback failed: %s", type(e).__name__)
            raise TranscriptFailure("transcript_failed", "Video understanding fallback failed.")
        text = resp.text or ""
        if "NO_SPEECH" in text[:50]:
            raise TranscriptFailure("no_transcript", "The video appears to contain no speech.", terminal=True)
        segs = []
        for m in re.finditer(r"^\s*\[(?:(\d+):)?(\d{1,2}):(\d{2})\]\s*(.+)$", text, re.M):
            h, mi, se, t = m.groups()
            segs.append(Seg(_clean(t), int(h or 0) * 3600 + int(mi) * 60 + int(se)))
        for a, b in zip(segs, segs[1:]):
            a.dur = max(b.start - a.start, 0)
        if not segs:
            raise TranscriptFailure("no_transcript", FAIL_MESSAGES["no_transcript"])
        return Transcript(segs, langs[0] if langs else "", "model_generated", self.name, [])


# ----------------------------------------------------------------------------- metadata
_CHAPTER = re.compile(r"^\s*(?:[-•▪►▶]\s*)?\(?((?:\d{1,2}:)?\d{1,2}:\d{2})\)?\s*[-–—:|]?\s*(.{2,120})$")


def parse_chapters(description: str) -> list[tuple[float, str]]:
    """YouTube's own rule: first timestamp 0:00, at least 3 chapters, ascending."""
    items = []
    for line in (description or "").splitlines():
        m = _CHAPTER.match(line)
        if m:
            parts = [int(x) for x in m.group(1).split(":")]
            secs = parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]
            items.append((float(secs), m.group(2).strip()))
    if len(items) >= 3 and items[0][0] == 0 and all(a[0] < b[0] for a, b in zip(items, items[1:])):
        return items
    return []


def fetch_metadata(video_id: str, s: Settings, fetcher: SafeFetcher) -> VideoMeta:
    meta = VideoMeta(video_id)
    if s.youtube_api_key:
        try:
            r = httpx.get("https://www.googleapis.com/youtube/v3/videos",
                          params={"part": "snippet,contentDetails", "id": video_id, "key": s.youtube_api_key}, timeout=15)
            items = r.json().get("items") if r.status_code == 200 else None
            if items:
                sn = items[0]["snippet"]
                meta.title, meta.author = sn.get("title", ""), sn.get("channelTitle", "")
                meta.description = sn.get("description", "")
                meta.is_live = sn.get("liveBroadcastContent") in ("live", "upcoming")
                m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", items[0]["contentDetails"].get("duration", ""))
                if m:
                    meta.duration = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)
        except Exception as e:
            log.info("youtube data api failed: %s", type(e).__name__)
    if not meta.title:
        try:
            r = fetcher.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
                            max_bytes=200_000, accept="application/json")
            import json
            d = json.loads(r.text)
            meta.title, meta.author = d.get("title", ""), d.get("author_name", "")
        except AppError as e:
            if e.code in ("not_found", "login_required", "blocked_by_site"):
                # oEmbed returns 401/404 for private/removed/embedding-disabled videos
                meta.title = ""
        except Exception:
            pass
    meta.chapters = parse_chapters(meta.description)
    return meta


def _clean(t: str) -> str:
    t = html.unescape(t or "").replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def expand_playlist(playlist_id: str, s: Settings) -> list[str]:
    ids: list[str] = []
    if s.youtube_api_key:
        page = None
        while len(ids) < s.youtube_max_playlist_videos:
            params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50, "key": s.youtube_api_key}
            if page:
                params["pageToken"] = page
            r = httpx.get("https://www.googleapis.com/youtube/v3/playlistItems", params=params, timeout=15)
            if r.status_code != 200:
                break
            d = r.json()
            ids += [i["contentDetails"]["videoId"] for i in d.get("items", [])]
            page = d.get("nextPageToken")
            if not page:
                break
    if not ids:
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "skip_download": True, "socket_timeout": 20}) as y:
                info = y.extract_info(f"https://www.youtube.com/playlist?list={playlist_id}", download=False)
            ids = [e["id"] for e in info.get("entries", []) if e.get("id")]
        except Exception:
            pass
    if not ids:
        raise IngestError("Couldn't read that playlist. It may be private, or the server needs YOUTUBE_API_KEY "
                          "(or yt-dlp) to expand playlists.", code="playlist_failed")
    return [i for i in dict.fromkeys(ids) if _ID.match(i)][: s.youtube_max_playlist_videos]


# ----------------------------------------------------------------------------- orchestration
def build_providers(s: Settings, fetcher: SafeFetcher) -> list:
    provs = [TranscriptApiProvider(s)]
    if s.supadata_api_key:
        provs.append(SupadataProvider(s))
    if s.enable_ytdlp_fallback:
        provs.append(YtDlpProvider(s, fetcher))
    if s.enable_gemini_video_fallback and s.gemini_api_key:
        provs.append(GeminiVideoProvider(s))
    return provs


def ingest_youtube(video_id: str, s: Settings, fetcher: SafeFetcher, langs: list[str] | None = None,
                   providers: list | None = None, meta_fn: Callable[[str], VideoMeta] | None = None,
                   progress: Callable[[float, str], None] = lambda f, m: None) -> Extracted:
    langs = langs or ["en"]
    progress(0.05, "Reading video details")
    meta = (meta_fn or (lambda v: fetch_metadata(v, s, fetcher)))(video_id)
    if meta.is_live:
        raise IngestError(FAIL_MESSAGES["live_stream"], code="live_stream")
    providers = providers if providers is not None else build_providers(s, fetcher)
    attempts: list[dict] = []
    transcript: Transcript | None = None
    terminal: TranscriptFailure | None = None
    first_blocking: TranscriptFailure | None = None
    for i, prov in enumerate(providers):
        progress(0.15 + 0.5 * i / max(len(providers), 1), f"Fetching transcript ({prov.name})")
        try:
            tr = prov.fetch(video_id, langs)
        except TranscriptFailure as f:
            attempts.append({"provider": prov.name, "ok": False, "code": f.code})
            if f.code == "not_configured":
                continue
            if f.terminal and terminal is None:
                terminal = f
            if f.code == "blocked_by_youtube" and first_blocking is None:
                first_blocking = f
            if f.terminal and f.code in ("video_unavailable", "age_restricted"):
                break  # properties of the video itself; no other provider can help
            continue
        except Exception as e:
            attempts.append({"provider": prov.name, "ok": False, "code": "error"})
            log.info("provider %s crashed: %s", prov.name, type(e).__name__)
            continue
        if len(" ".join(x.text for x in tr.segments).strip()) < 40:
            attempts.append({"provider": prov.name, "ok": False, "code": "too_short"})
            continue
        attempts.append({"provider": prov.name, "ok": True, "segments": len(tr.segments)})
        transcript = tr
        break
    if transcript is None:
        # a gemini-video attempt can rescue "no captions" but not "unavailable"
        f = terminal or first_blocking
        code = f.code if f else "transcript_failed"
        raise IngestError(FAIL_MESSAGES.get(code, "Couldn't retrieve a transcript for this video."), code=code,
                          detail={"attempts": attempts, "video_id": video_id})

    segs = _repair_timestamps(transcript.segments)
    blocks: list[Block] = []
    chapters = meta.chapters
    ci = 0
    for sg in segs:
        while ci < len(chapters) and chapters[ci][0] <= sg.start + 0.5:
            blocks.append(Block("heading", chapters[ci][1], level=2, t_start=chapters[ci][0], meta={"chapter": True}))
            ci += 1
        blocks.append(Block("transcript", sg.text, t_start=sg.start, t_end=sg.start + (sg.dur or 0)))
    warnings = []
    if transcript.kind == "generated":
        warnings.append("Transcript is auto-generated by YouTube: names, numbers and technical terms may be mis-heard.")
    if transcript.kind == "model_generated":
        warnings.append("No captions exist for this video, so the transcript was generated by an AI model "
                        "(Gemini video understanding). It may contain errors and its timestamps are approximate.")
    if langs and transcript.language and not transcript.language.lower().startswith(langs[0].lower()):
        warnings.append(f"Requested language '{langs[0]}' wasn't available; using '{transcript.language}'.")
    last = segs[-1]
    covered = last.start + last.dur
    if meta.duration and covered < 0.6 * meta.duration:
        warnings.append(f"The transcript covers only {int(covered // 60)} of {int(meta.duration // 60)} minutes; "
                        f"answers about later parts of the video are not possible.")
    title = meta.title or f"YouTube video {video_id}"
    return Extracted(
        title=title, blocks=blocks, n_pages=1, warnings=warnings, partial=False,
        diagnostics={"extractor": "youtube", "provider": transcript.provider, "attempts": attempts,
                     "language": transcript.language, "transcript_kind": transcript.kind,
                     "available_languages": transcript.available_languages, "segments": len(segs),
                     "chapters": len(chapters), "duration_s": meta.duration, "covered_s": int(covered)},
        meta={"video_id": video_id, "channel": meta.author, "duration": meta.duration, "language": transcript.language,
              "transcript_kind": transcript.kind, "modality": "transcript", "provider": transcript.provider})


def _repair_timestamps(segs: list[Seg]) -> list[Seg]:
    """Auto-captions sometimes overlap or repeat; keep order, drop exact consecutive duplicates."""
    out: list[Seg] = []
    for s_ in segs:
        if out and out[-1].text == s_.text and abs(out[-1].start - s_.start) < 1:
            continue
        out.append(s_)
    out.sort(key=lambda x: x.start)
    return out
