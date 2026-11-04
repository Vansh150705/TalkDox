"""OCR engines for scanned PDF pages.

* `TesseractOCR` runs locally (no data leaves the server) if the `tesseract` binary exists.
* `GeminiOCR` sends the page IMAGE to Google's Gemini API (documented in docs/PRIVACY.md).
* `FakeOCR` is a test double.
OCR output is untrusted text exactly like any other extracted content.
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from typing import Callable, Optional

from ..config import Settings

log = logging.getLogger("talkdox.ocr")

OCR_PROMPT = (
    "Transcribe ALL text on this page image exactly as written, in natural reading order. "
    "Render tables as GitHub-flavoured markdown tables. Mark headings with '#'. "
    "Do not summarise, translate, explain or add anything. If the page has no readable text, reply with exactly: [NO TEXT]"
)


class BaseOCR:
    name = "none"

    def page_to_text(self, image) -> str:  # PIL.Image
        raise NotImplementedError


class TesseractOCR(BaseOCR):
    name = "tesseract"

    def __init__(self, lang: str = "eng"):
        self.lang = lang

    def page_to_text(self, image) -> str:
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            image.save(f, format="PNG")
            f.flush()
            r = subprocess.run(["tesseract", f.name, "stdout", "-l", self.lang], capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError("tesseract failed")
        return r.stdout.decode("utf-8", "replace")


class GeminiOCR(BaseOCR):
    name = "gemini"

    def __init__(self, settings: Settings):
        from google import genai
        from google.genai import types
        self._types = types
        self._client = genai.Client(api_key=settings.gemini_api_key,
                                    http_options=types.HttpOptions(
                                        timeout=int(settings.llm_timeout_s * 1000),
                                        **({"base_url": settings.gemini_base_url} if settings.gemini_base_url else {})))
        self.model = settings.gemini_model

    def page_to_text(self, image) -> str:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        t = self._types
        resp = self._client.models.generate_content(
            model=self.model,
            contents=[t.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"), OCR_PROMPT],
            config=t.GenerateContentConfig(temperature=0.0, max_output_tokens=8192,
                                           thinking_config=t.ThinkingConfig(thinking_budget=0)
                                           if "2.5" in self.model else None))
        text = (resp.text or "").strip()
        return "" if text == "[NO TEXT]" else text


class FakeOCR(BaseOCR):
    name = "fake"

    def __init__(self, fn: Callable[[object], str] | None = None):
        self.fn = fn or (lambda img: "")
        self.calls = 0

    def page_to_text(self, image) -> str:
        self.calls += 1
        return self.fn(image)


def build_ocr(settings: Settings) -> Optional[BaseOCR]:
    e = settings.ocr_engine
    if e == "none":
        return None
    if e in ("auto", "tesseract") and shutil.which("tesseract"):
        return TesseractOCR()
    if e in ("auto", "gemini") and settings.gemini_api_key:
        return GeminiOCR(settings)
    return None
