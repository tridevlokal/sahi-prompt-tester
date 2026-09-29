"""Gemini STT + TTS as pipecat services (Gemini API key auth).

- GeminiInteractionsTTSService : Gemini 3.8 Flash / Flash-Lite TTS via the Interactions
  API (streaming SSE, PCM 24 kHz). Use this for the 3.8 models.

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
import json

import aiohttp
from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame, TTSAudioRawFrame
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator
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
        super().__init__(sample_rate=sample_rate,
                         settings=TTSSettings(model=model, voice=voice, language=None), **kwargs)
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
        # STREAMING (SSE): audio chunks arrive as they're synthesised, so playback
        # starts in ~1.5s instead of waiting ~7s for the whole clip. This is what
        # kills the long pause between sentences.
        url = f"{GENAI_BASE}/models/{self._model}:streamGenerateContent?alt=sse&key={self._api_key}"
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
                await self.start_tts_usage_metrics(text)

                first = True
                got_any = False
                async for raw in resp.content:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    for audio in self._iter_audio(data):
                        got_any = True
                        if first:
                            await self.stop_ttfb_metrics()
                            first = False
                        yield TTSAudioRawFrame(audio, self.sample_rate, 1, context_id=context_id)

                if not got_any:
                    yield ErrorFrame(error="Gemini TTS: no audio in stream")
        except Exception as e:
            yield ErrorFrame(error=f"Gemini TTS failed: {e}")

    @staticmethod
    def _iter_audio(data: dict):
        """Yield decoded PCM from every audio part in a (streaming) response chunk."""
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            return
        for p in parts:
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and inline.get("data"):
                yield base64.b64decode(inline["data"])


class WholeResponseAggregator(SimpleTextAggregator):
    """Buffer every LLM token and release the whole reply once, on LLMFullResponseEnd.

    Gemini TTS has ~1.5 s TTFB per request, so pipecat's default per-sentence
    splitting (at . ? ! and the Hindi danda) puts a 1.5 s hole after every sentence.
    One request per reply = one TTFB, no holes, and the model gets full-paragraph
    prosody. Riya's replies are 2-4 sentences, so the extra wait before the first
    word is small."""

    async def aggregate(self, text: str):
        self._text += text
        return
        yield  # keep this an async generator


class GeminiInteractionsTTSService(TTSService):
    """Gemini 3.8 Flash TTS / Flash-Lite TTS over the Interactions API (Gemini API key).

    POST /v1beta/interactions with stream=true -> SSE. Each `step.delta` event carries
    a base64 chunk of headerless PCM16 mono at the requested sample_rate (we ask for
    24 kHz, the model's native rate). Language is auto-detected from the text (there
    is no language field), so native-script text is what selects the accent.
    `style` rides along as a speech_metadata annotation ("warm, friendly teacher").
    Verified 25 Sep 2026: TTFB ~1.5 s (lite) / ~1.9 s (flash) for a 2-sentence Telugu line.
    """

    INTERACTIONS_URL = f"{GENAI_BASE}/interactions"

    def __init__(self, *, api_key: str, aiohttp_session: aiohttp.ClientSession,
                 model: str = "gemini-3.8-flash-lite-tts",
                 voice: str = "Aoede",
                 style_prompt: str | None = None,
                 chunking: str = "response",
                 on_usage=None,
                 sample_rate: int = GEMINI_TTS_SAMPLE_RATE, **kwargs):
        super().__init__(sample_rate=sample_rate,
                         settings=TTSSettings(model=model, voice=voice, language=None), **kwargs)
        self._api_key = api_key
        self._session = aiohttp_session
        self._model = model
        self._voice = voice
        self._style = (style_prompt or "").strip()
        # "response" = one TTS request per LLM reply (default, no inter-sentence gaps);
        # "sentence" = pipecat default, one request per sentence (lower first-word latency).
        if chunking == "response":
            self._text_aggregator = WholeResponseAggregator()
        # on_usage(text_tokens, audio_tokens) from the interaction.completed event:
        # Gemini TTS bills per audio token, not per character, so the cost meter
        # needs the real count.
        self._on_usage = on_usage

    def can_generate_metrics(self) -> bool:
        return True

    def _body(self, text: str) -> dict:
        content = {"type": "text", "text": text}
        if self._style:
            content["annotations"] = [{"type": "speech_metadata", "style": self._style}]
        return {
            "model": self._model,
            "stream": True,
            "input": [{"type": "user_input", "content": [content]}],
            "response_format": {"type": "audio", "mime_type": "audio/l16",
                                "sample_rate": self.sample_rate},
            "generation_config": {"speech_config": [{"voice": self._voice}]},
        }

    async def _http(self) -> aiohttp.ClientSession:
        # run_bot only opens a shared session for the ElevenLabs path; own one otherwise.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    async def cleanup(self):
        await super().cleanup()
        if getattr(self, "_own_session", False) and self._session and not self._session.closed:
            await self._session.close()

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        logger.debug(f"{self}: Gemini 3.8 TTS [{text}]")
        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}
        try:
            await self.start_ttfb_metrics()
            session = await self._http()
            async with session.post(self.INTERACTIONS_URL, json=self._body(text),
                                    headers=headers) as resp:
                if resp.status != 200:
                    yield ErrorFrame(error=f"Gemini 3.8 TTS {resp.status}: {(await resp.text())[:200]}")
                    return
                await self.start_tts_usage_metrics(text)
                first = True
                got_any = False
                async for raw in resp.content:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    delta = data.get("delta") or {}
                    if not delta.get("data") or not str(delta.get("mime_type", "")).startswith("audio"):
                        usage = (data.get("interaction") or {}).get("usage")
                        if usage and self._on_usage:
                            try:
                                self._on_usage(int(usage.get("total_input_tokens", 0)),
                                               int(usage.get("total_output_tokens", 0)))
                            except Exception as e:
                                logger.warning(f"Gemini TTS usage callback failed: {e}")
                        continue
                    got_any = True
                    if first:
                        await self.stop_ttfb_metrics()
                        first = False
                    yield TTSAudioRawFrame(base64.b64decode(delta["data"]), self.sample_rate, 1,
                                           context_id=context_id)
                if not got_any:
                    yield ErrorFrame(error="Gemini 3.8 TTS: no audio in stream")
        except Exception as e:
            yield ErrorFrame(error=f"Gemini 3.8 TTS failed: {e}")


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
