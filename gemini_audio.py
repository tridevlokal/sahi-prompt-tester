"""Gemini 3.1 Flash STT + TTS as pipecat services (Gemini API key auth).

pipecat's built-in GeminiTTSService / GoogleSTTService need a GCP service-account
JSON. These two use the plain **Gemini API key** (generativelanguage endpoint) and
the Gemini 3.1 Flash models, so they drop into the existing pipeline with the same
GOOGLE_API_KEY the LLM already uses.

- GeminiFlashTTSService : text -> gemini-3.1-flash-tts-preview (generateContent,
  responseModalities=["AUDIO"]) -> PCM 24 kHz mono. Supports a style prompt.
- GeminiFlashSTTService : one utterance of WAV audio -> gemini-3.1-flash
  (generateContent, audio inlineData) -> transcript. Batch (per turn), any language.
"""
import base64

import aiohttp
from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame, TTSAudioRawFrame
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601
from typing import AsyncGenerator

GENAI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Gemini TTS native output is PCM signed-16 little-endian, 24 kHz, mono.
GEMINI_TTS_SAMPLE_RATE = 24000


class GeminiFlashTTSService(TTSService):
    """Gemini 3.1 Flash TTS over the Gemini API key (non-streaming generateContent)."""

    def __init__(self, *, api_key: str, aiohttp_session: aiohttp.ClientSession,
                 model: str = "gemini-3.1-flash-tts-preview",
                 voice: str = "Kore",
                 style_prompt: str | None = None,
                 sample_rate: int = GEMINI_TTS_SAMPLE_RATE, **kwargs):
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._api_key = api_key
        self._session = aiohttp_session
        self._model = model
        self._voice = voice
        self._style = (style_prompt or "").strip()

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        logger.debug(f"{self}: Gemini TTS [{text}]")
        # A style prompt steers delivery ("Say warmly and slowly: <text>"). Gemini
        # speaks the text after the instruction, not the instruction itself.
        prompt = f"{self._style}: {text}" if self._style else text
        url = f"{GENAI_BASE}/models/{self._model}:generateContent?key={self._api_key}"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self._voice}}
                },
            },
        }
        try:
            await self.start_ttfb_metrics()
            async with self._session.post(url, json=body) as resp:
                if resp.status != 200:
                    yield ErrorFrame(error=f"Gemini TTS {resp.status}: {(await resp.text())[:200]}")
                    return
                data = await resp.json()

            await self.start_tts_usage_metrics(text)
            audio = self._extract_audio(data)
            if not audio:
                yield ErrorFrame(error=f"Gemini TTS: no audio in response")
                return

            await self.stop_ttfb_metrics()
            # The whole clip arrives at once (non-streaming); chunk it so the output
            # transport pipelines playback smoothly (0.2s frames @ 24k/16-bit mono).
            chunk = int(self.sample_rate * 2 * 0.2)
            for i in range(0, len(audio), chunk):
                yield TTSAudioRawFrame(audio[i:i + chunk], self.sample_rate, 1, context_id=context_id)
        except Exception as e:
            yield ErrorFrame(error=f"Gemini TTS failed: {e}")

    @staticmethod
    def _extract_audio(data: dict) -> bytes:
        try:
            for p in data["candidates"][0]["content"]["parts"]:
                inline = p.get("inlineData") or p.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
        except (KeyError, IndexError, TypeError):
            pass
        return b""


class GeminiFlashSTTService(SegmentedSTTService):
    """Gemini 3.1 Flash STT: each VAD-committed utterance (WAV) -> transcript.

    Batch, not streaming — the base class buffers audio between speech start/stop and
    hands us one WAV blob per turn. Higher latency than a realtime STT, but multilingual
    (Hindi/Telugu/Tamil/… + English) with no language lock needed.
    """

    def __init__(self, *, api_key: str, aiohttp_session: aiohttp.ClientSession,
                 model: str = "gemini-3.1-flash-lite",
                 sample_rate: int | None = None, **kwargs):
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._api_key = api_key
        self._session = aiohttp_session
        self._model = model

    def can_generate_metrics(self) -> bool:
        return True

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        url = f"{GENAI_BASE}/models/{self._model}:generateContent?key={self._api_key}"
        body = {
            "contents": [{"parts": [
                {"inlineData": {"mimeType": "audio/wav",
                                "data": base64.b64encode(audio).decode("utf-8")}},
                {"text": "Transcribe this audio verbatim, in its original language and "
                         "native script. Output ONLY the transcript text — no quotes, no "
                         "translation, no extra words. If there is no clear speech, output nothing."},
            ]}],
            "generationConfig": {"temperature": 0.0},
        }
        try:
            await self.start_processing_metrics()
            async with self._session.post(url, json=body) as resp:
                if resp.status != 200:
                    yield ErrorFrame(error=f"Gemini STT {resp.status}: {(await resp.text())[:200]}")
                    return
                data = await resp.json()

            text = self._extract_text(data).strip()
            if text:
                logger.debug(f"{self}: Gemini STT [{text}]")
                yield TranscriptionFrame(text, self._user_id, time_now_iso8601(), "auto")
        except Exception as e:
            yield ErrorFrame(error=f"Gemini STT failed: {e}")

    @staticmethod
    def _extract_text(data: dict) -> str:
        try:
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError):
            return ""
