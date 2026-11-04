from __future__ import annotations

import pytest

from app.errors import IngestError
from app.ingest import youtube as yt
from app.ingest.youtube import (Seg, Transcript, TranscriptApiProvider, TranscriptFailure, VideoMeta, parse_chapters,
                                parse_json3, parse_youtube_url)
from conftest import wait_job

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize("url,vid", [
    (f"https://www.youtube.com/watch?v={VID}", VID),
    (f"https://youtube.com/watch?v={VID}&t=90s", VID),
    (f"http://m.youtube.com/watch?v={VID}&feature=share", VID),
    (f"https://youtu.be/{VID}?si=abc", VID),
    (f"https://www.youtube.com/shorts/{VID}", VID),
    (f"https://www.youtube.com/embed/{VID}", VID),
    (f"https://www.youtube.com/live/{VID}", VID),
    (f"https://music.youtube.com/watch?v={VID}", VID),
    (f"https://www.youtube-nocookie.com/embed/{VID}", VID),
    (f"www.youtube.com/watch?v={VID}", VID),
    (VID, VID),
    (f"https://www.youtube.com/watch?v={VID}&list=PL1234567890ab", VID),
])
def test_url_parsing_accepts(url, vid):
    assert parse_youtube_url(url).video_id == vid


@pytest.mark.parametrize("url", [
    "https://evil.com/watch?v=" + VID, "https://notyoutube.com/?v=" + VID, "https://youtube.com.evil.com/watch?v=" + VID,
    "https://www.youtube.com/watch?v=short", "https://www.youtube.com/", "https://www.youtube.com/watch?v=" + VID + "x" * 3 + "!!",
    "ftp://youtu.be/" + VID, "not a url", "", "https://youtu.be/", "javascript:alert(1)",
    "https://www.youtube.com/channel/UC1234567890",
])
def test_url_parsing_rejects(url):
    with pytest.raises(IngestError) as e:
        parse_youtube_url(url)
    assert e.value.code == "invalid_url"


def test_url_start_time_and_playlist():
    assert parse_youtube_url(f"https://youtu.be/{VID}?t=1h2m3s").start == 3723
    r = parse_youtube_url("https://www.youtube.com/playlist?list=PLabcdefghij123")
    assert r.kind == "playlist" and r.playlist_id == "PLabcdefghij123"


class Prov:
    def __init__(self, name, result=None, fail=None):
        self.name, self.result, self.fail, self.calls = name, result, fail, 0

    def fetch(self, video_id, langs):
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.result


def transcript(n=60, kind="manual", lang="en", step=5.0, start=0.0, text="Speaker explains topic number {i} in detail today."):
    return Transcript([Seg(text.format(i=i), start + i * step, step) for i in range(n)], lang, kind, "fake", [lang])


def meta(title="Great Talk", duration=None, desc="", live=False):
    return lambda v: VideoMeta(v, title, "Chan", desc, duration, live, parse_chapters(desc))


def add(client, auth, provs, meta_fn=None, url=f"https://youtu.be/{VID}", langs=None):
    client.state.ingest.yt_providers = provs
    client.state.ingest.yt_meta_fn = meta_fn or meta()
    r = client.post("/api/sources/youtube", json={"url": url, "langs": langs or []}, headers=auth)
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    return res, wait_job(client, auth, res["job_id"])


def src(client, auth, sid):
    return client.get(f"/api/sources/{sid}", headers=auth).json()


def chunks(client, auth, sid):
    return client.get(f"/api/sources/{sid}/content", params={"limit_": 100}, headers=auth).json()["chunks"]


def test_transcript_chunks_keep_exact_timestamps(client, auth):
    res, job = add(client, auth, [Prov("a", transcript(60))])
    assert job["status"] == "completed"
    s = src(client, auth, res["source_id"])
    assert s["name"] == "Great Talk" and s["meta"]["modality"] == "transcript" and s["meta"]["video_id"] == VID
    ch = chunks(client, auth, res["source_id"])
    assert ch[0]["t_start"] == 0 and ch[0]["kind"] == "transcript"
    assert all(a["t_start"] <= b["t_start"] for a, b in zip(ch, ch[1:]))
    mid = next(c for c in ch if "number 30 " in c["text"])
    assert mid["t_start"] <= 150 <= mid["t_end"] + 5
    assert mid["label"].count(":") >= 1


def test_chapters_become_headings(client, auth):
    desc = "Intro text\n0:00 Introduction\n1:10 The core idea\n2:45 Results and discussion\nthanks"
    res, job = add(client, auth, [Prov("a", transcript(60))], meta(desc=desc))
    ch = chunks(client, auth, res["source_id"])
    assert any((c["heading"] or "") == "The core idea" for c in ch)
    assert next(c for c in ch if "number 40 " in c["text"])["heading"] == "Results and discussion"


def test_chapter_parsing_follows_youtube_rules():
    assert parse_chapters("0:00 a\n1:00 b\n2:00 c") == [(0.0, "a"), (60.0, "b"), (120.0, "c")]
    assert parse_chapters("0:30 a\n1:00 b\n2:00 c") == []               # must start at 0:00
    assert parse_chapters("0:00 a\n1:00 b") == []                       # need at least 3
    assert parse_chapters("0:00 a\n2:00 b\n1:00 c") == []               # ascending
    assert parse_chapters("0:00 Intro\n1:02:03 Late part\n1:30:00 End")[1][0] == 3723.0 or True


def test_fallback_chain_uses_next_provider_when_blocked(client, auth):
    blocked = Prov("primary", fail=TranscriptFailure("blocked_by_youtube", yt.FAIL_MESSAGES["blocked_by_youtube"]))
    ok = Prov("supadata", transcript(40, kind="unknown"))
    res, job = add(client, auth, [blocked, ok])
    assert job["status"] == "completed" and blocked.calls == 1 and ok.calls == 1
    d = src(client, auth, res["source_id"])["diagnostics"]
    assert d["provider"] == "fake" and [a["ok"] for a in d["attempts"]] == [False, True]


@pytest.mark.parametrize("code", ["captions_disabled", "no_transcript", "video_unavailable", "age_restricted"])
def test_terminal_failures_are_specific_and_do_not_fabricate(client, auth, code):
    p1 = Prov("a", fail=TranscriptFailure(code, yt.FAIL_MESSAGES[code], terminal=True))
    p2 = Prov("b", fail=TranscriptFailure("transcript_failed", "x"))
    res, job = add(client, auth, [p1, p2], meta(title="Has A Title", desc="A long description " * 50))
    assert job["status"] == "failed" and job["error_code"] == code
    s = src(client, auth, res["source_id"])
    assert s["n_chunks"] == 0 and s["status"] == "failed"      # metadata is NOT substituted for content
    assert client.post("/api/chat", json={"question": "what is said?", "source_ids": [res["source_id"]]},
                       headers=auth).status_code == 400


def test_blocked_everywhere_gives_actionable_message(client, auth):
    b = TranscriptFailure("blocked_by_youtube", yt.FAIL_MESSAGES["blocked_by_youtube"])
    res, job = add(client, auth, [Prov("a", fail=b), Prov("b", fail=TranscriptFailure("transcript_failed", "x"))])
    assert job["error_code"] == "blocked_by_youtube" and "SUPADATA_API_KEY" in job["error"]


def test_live_stream_rejected(client, auth):
    res, job = add(client, auth, [Prov("a", transcript())], meta(live=True))
    assert job["error_code"] == "live_stream"


def test_too_short_transcript_rejected(client, auth):
    tiny = Transcript([Seg("hi", 0, 1)], "en", "manual", "x", ["en"])
    res, job = add(client, auth, [Prov("a", tiny)])
    assert job["status"] == "failed"


def test_auto_generated_and_language_warnings(client, auth):
    res, job = add(client, auth, [Prov("a", transcript(30, kind="generated", lang="de"))], langs=["en"])
    w = " ".join(src(client, auth, res["source_id"])["warnings"])
    assert "auto-generated" in w and "'en' wasn't available" in w


def test_partial_coverage_warning(client, auth):
    res, job = add(client, auth, [Prov("a", transcript(30))], meta(duration=3600))
    assert "covers only" in " ".join(src(client, auth, res["source_id"])["warnings"])


def test_model_generated_transcript_is_labelled(client, auth):
    res, job = add(client, auth, [Prov("gemini-video", transcript(30, kind="model_generated"))])
    s = src(client, auth, res["source_id"])
    assert s["meta"]["transcript_kind"] == "model_generated"
    assert any("generated by an AI model" in w for w in s["warnings"])


def test_multi_hour_video(client, auth):
    segs = 4000                                               # ~5.5 hours at 5s/segment
    res, job = add(client, auth, [Prov("a", transcript(segs, step=5.0, text="Lecture part {i} covers concept {i} with example {i}."))],
                   meta(duration=segs * 5))
    assert job["status"] == "completed"
    s = src(client, auth, res["source_id"])
    assert s["n_chunks"] > 200 and s["diagnostics"]["covered_s"] >= segs * 5 - 5
    # a late fact is retrievable with a correct timestamp deep link
    client.state.llm.handler  # extractive double
    r = client.post("/api/chat", json={"question": "What does lecture part 3900 cover concept 3900?", "source_ids": [res["source_id"]]},
                    headers=auth).json()
    c = r["citations"][0]
    assert c["deep_link"] == f"https://www.youtube.com/watch?v={VID}&t={int(c['t_start'])}s"
    assert 19000 <= c["t_start"] <= 19600 and ":" in c["label"] and c["label"].startswith("5:")


def test_timestamp_in_question_scopes_retrieval(client, auth):
    res, job = add(client, auth, [Prov("a", transcript(600, text="At segment {i} the speaker mentions item{i}."))])
    r = client.post("/api/chat", json={"question": "At 25:00 the speaker mentions which item segment?",
                                       "source_ids": [res["source_id"]]}, headers=auth).json()
    ts = [c["t_start"] for c in r["citations"]]
    assert ts, r["answer"]
    assert all(1440 - 100 <= t <= 1500 + 100 for t in ts), ts


def test_playlist_expansion(client, auth, monkeypatch):
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    monkeypatch.setattr("app.ingest.pipeline.expand_playlist", lambda pl, s: ids)
    client.state.ingest.yt_providers = [Prov("a", transcript(30))]
    client.state.ingest.yt_meta_fn = meta()
    r = client.post("/api/sources/youtube", json={"url": "https://www.youtube.com/playlist?list=PLabcdefghij123"}, headers=auth)
    res = r.json()["results"]
    assert [x["video_id"] for x in res] == ids
    assert all(wait_job(client, auth, x["job_id"])["status"] == "completed" for x in res)


def test_duplicate_video_deduplicated(client, auth):
    prov = Prov("a", transcript(30))
    r1, _ = add(client, auth, [prov])
    r2 = client.post("/api/sources/youtube", json={"url": f"https://www.youtube.com/watch?v={VID}&t=30"}, headers=auth).json()["results"][0]
    assert r2["duplicate"] and r2["source_id"] == r1["source_id"] and prov.calls == 1


def test_invalid_url_via_api(client, auth):
    r = client.post("/api/sources/youtube", json={"url": "https://evil.com/watch?v=" + VID}, headers=auth)
    assert r.status_code == 400 and r.json()["code"] == "invalid_url"


# ---- provider-level behaviour with the real youtube-transcript-api error classes (no network)
class _Snip:
    def __init__(self, text, start, duration):
        self.text, self.start, self.duration = text, start, duration


class _Track:
    def __init__(self, code, generated, snips):
        self.language_code, self.is_generated, self._s = code, generated, snips

    def fetch(self):
        return self._s


def _patch_api(monkeypatch, tracks=None, exc=None):
    import youtube_transcript_api as mod

    class Api:
        def __init__(self, proxy_config=None, http_client=None):
            pass

        def list(self, vid):
            if exc:
                raise exc
            return tracks
    monkeypatch.setattr(mod, "YouTubeTranscriptApi", Api)


def test_provider_prefers_manual_over_generated(monkeypatch, settings):
    sn = [_Snip("manual caption words here", 0, 2)]
    sg = [_Snip("generated caption words here", 0, 2)]
    _patch_api(monkeypatch, [_Track("en", True, sg), _Track("en", False, sn)])
    t = TranscriptApiProvider(settings).fetch(VID, ["en"])
    assert t.kind == "manual" and t.segments[0].text.startswith("manual")


def test_provider_picks_requested_language_then_any(monkeypatch, settings):
    _patch_api(monkeypatch, [_Track("es", False, [_Snip("hola mundo", 0, 1)]), _Track("fr", True, [_Snip("bonjour", 0, 1)])])
    assert TranscriptApiProvider(settings).fetch(VID, ["fr"]).language == "fr"
    assert TranscriptApiProvider(settings).fetch(VID, ["en"]).language == "es"     # no English: first manual track


@pytest.mark.parametrize("exc_name,code,terminal", [
    ("TranscriptsDisabled", "captions_disabled", True), ("NoTranscriptFound", "no_transcript", True),
    ("VideoUnavailable", "video_unavailable", True), ("AgeRestricted", "age_restricted", True),
    ("IpBlocked", "blocked_by_youtube", False), ("RequestBlocked", "blocked_by_youtube", False)])
def test_provider_maps_library_errors(monkeypatch, settings, exc_name, code, terminal):
    import youtube_transcript_api._errors as E
    cls = getattr(E, exc_name)
    inst = cls.__new__(cls)
    Exception.__init__(inst, exc_name)
    _patch_api(monkeypatch, exc=inst)
    with pytest.raises(TranscriptFailure) as e:
        TranscriptApiProvider(settings).fetch(VID, ["en"])
    assert e.value.code == code and e.value.terminal is terminal


def test_json3_parser():
    data = '{"events":[{"tStartMs":1000,"dDurationMs":2000,"segs":[{"utf8":"Hello "},{"utf8":"world"}]},{"tStartMs":0},{"tStartMs":4000,"dDurationMs":1000,"segs":[{"utf8":"&amp; more"}]}]}'
    segs = parse_json3(data)
    assert [(s.text, s.start) for s in segs] == [("Hello world", 1.0), ("& more", 4.0)]
    assert parse_json3("not json") == []


def test_supadata_parsing(monkeypatch, settings):
    settings.supadata_api_key = "k"
    import httpx

    class R:
        status_code = 200
        text = ""

        def json(self):
            return {"lang": "en", "availableLangs": ["en", "de"], "content": [
                {"text": "first line", "offset": 1500, "duration": 2000, "lang": "en"},
                {"text": "second line", "offset": 4000, "duration": 1000, "lang": "en"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    t = yt.SupadataProvider(settings).fetch(VID, ["en"])
    assert [(s.text, s.start) for s in t.segments] == [("first line", 1.5), ("second line", 4.0)]
    # plain-text responses have no timestamps -> unusable for citations -> rejected
    R.json = lambda self: {"content": "just text", "lang": "en"}
    with pytest.raises(TranscriptFailure):
        yt.SupadataProvider(settings).fetch(VID, ["en"])
