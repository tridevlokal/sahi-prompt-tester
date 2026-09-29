"""
bot.py — Pipecat voice pipeline for AI Riya (ElevenLabs-only).

Exposes run_bot(websocket, system_prompt, voice_id, *, speed, temperature):
  - Wraps an already-accepted FastAPI WebSocket in a Pipecat transport
  - Builds an ElevenLabs STT (Scribe realtime) -> Gemini LLM -> ElevenLabs TTS pipeline
  - Sends caption JSON over the same WebSocket (text frames; audio is binary)
  - Runs until the browser disconnects

All tuning (models, VAD, TTS voice settings, temperature, interruptions) is read
live from settings_store.get_all() on each call, so the /settings page controls
the pipeline without a code change. Sarvam has been removed — STT and TTS are
both ElevenLabs now.
"""

import asyncio
import json
import os
import random
from pathlib import Path
import re
import time

import aiohttp
from fastapi import WebSocket
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    OutputAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    LLMRunFrame,
    LLMTextFrame,
    MetricsFrame,
    TranscriptionFrame,
)
from pipecat.metrics.metrics import LLMUsageMetricsData, TTFBMetricsData, TTSUsageMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.services.elevenlabs.stt import (
    CommitStrategy,
    ElevenLabsRealtimeSTTService,
    ElevenLabsSTTService,
)
from pipecat.services.elevenlabs.tts import (
    ElevenLabsHttpTTSService,
    ElevenLabsTTSService,
    ElevenLabsTTSSettings,
)
from pipecat.services.smallest.tts import SmallestTTSService
from pipecat.services.google.llm import GoogleLLMService, GoogleLLMSettings
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.turns.user_start.transcription_user_turn_start_strategy import (
    TranscriptionUserTurnStartStrategy,
)
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

import settings_store
from gemini_audio import GeminiFlashTTSService, GeminiInteractionsTTSService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.services.groq.stt import GroqSTTService
from pipecat.services.groq.llm import GroqLLMService, GroqLLMSettings
from pipecat.services.google.stt import GoogleSTTService
from pipecat.services.google.tts import GoogleTTSService


# Chirp3-HD reads a lowercase standalone "us" as the abbreviation "U.S." ("you-ess"),
# and it supports no SSML/<sub> to correct it. Cheapest reliable fix: respell just
# that token as "uss" (same /ʌs/ sound) right before synthesis. Word-boundary +
# lowercase-only, so "US"/"U.S." (the country), "use", "bus", "discuss" are untouched.
# TTS input only — captions/LLM context keep the original text.
_US_FIX = re.compile(r"\bus\b")


class GoogleChirpTTSService(GoogleTTSService):
    async def run_tts(self, text: str, context_id: str):
        async for frame in super().run_tts(_US_FIX.sub("uss", text), context_id):
            yield frame
from pipecat.transcriptions.language import Language

# Picker language code -> Google Cloud Language (STT) + BCP-47 code (Chirp3-HD voice ids).
GOOGLE_LANG = {
    "hin": Language.HI_IN, "tel": Language.TE_IN, "tam": Language.TA_IN,
    "kan": Language.KN_IN, "mal": Language.ML_IN, "ben": Language.BN_IN,
    "eng": Language.EN_US,
}
GOOGLE_LANG_CODE = {
    "hin": "hi-IN", "tel": "te-IN", "tam": "ta-IN",
    "kan": "kn-IN", "mal": "ml-IN", "ben": "bn-IN", "eng": "en-US",
}

INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000


# Text that is only punctuation / whitespace (e.g. a lone "." split off from
# "My name is...") — synthesising it just inserts a dead gap and makes speech
# sound slow and choppy. We skip these fragments entirely.
_PUNCT_ONLY = re.compile(r"^[\s.…,;:!?\-–—\"'“”‘’()\[\]]*$")

# Scripts the user actually speaks: Indian languages (Devanagari/Telugu/Tamil/
# Kannada/Malayalam) + English (Latin, incl. accents). With STT on auto-detect,
# noise/echo — especially the bot's own voice bleeding back in when interruptions
# are ON — gets hallucinated into whatever language the model guesses (Chinese,
# Korean, Russian/Cyrillic, …). Rather than blocklist each foreign script one by
# one, we ALLOWLIST the scripts we expect: any transcript dominated by characters
# outside this set is a detection hallucination and safe to drop. This keeps
# auto-detect (all Indian languages at once) without the junk.
_ALLOWED_SCRIPT = re.compile(
    r"[a-zA-Z"          # English (basic Latin)
    r"À-ɏ"    # Latin-1 supplement + extended (accented chars)
    r"ऀ-ॿ"    # Devanagari (Hindi)
    r"஀-௿"    # Tamil
    r"ఀ-౿"    # Telugu
    r"ಀ-೿"    # Kannada
    r"ഀ-ൿ"    # Malayalam
    r"ঀ-৿"    # Bengali
    r"]"
)


def _is_foreign_garbage(text: str) -> bool:
    """True if the transcript is dominated by scripts the user never speaks
    (Cyrillic, CJK, Arabic, …) — a language-detection hallucination on noise or
    echo, never real speech here. Real code-mixed Hindi+English stays (its letters
    are all Devanagari/Latin); a Russian mis-transcription is >40% foreign → dropped."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    allowed = sum(1 for c in letters if _ALLOWED_SCRIPT.match(c))
    return allowed / len(letters) < 0.6


# Whisper (Groq STT) invents YouTube-outro text on silence/noise that slips past VAD.
# Only multi-word phrases no learner says to Riya on a call — a bare "thank you" is
# real speech and stays.
_GHOST_TEXT = re.compile(
    r"thank(s| you) for watching|please subscribe|like and subscribe|subscribe to (my|our|the) channel"
    r"|subtitles? by|amara\.org|सब्सक्राइब|देखने के लिए धन्यवाद",
    re.IGNORECASE,
)


class SmallestFixedLangTTSService(SmallestTTSService):
    """Smallest.ai TTS that always sends a valid, non-null language string.

    pipecat's language map lacks Telugu/Malayalam, so those resolve to None — and
    Smallest's API rejects a null language with "Invalid input data". We store a
    plain language code (e.g. "te") and inject it directly into every message,
    bypassing the Language-enum round-trip entirely.

    We also skip punctuation-only fragments: ellipses ("...") in the LLM output
    get split into tiny pieces (including a lone "."), and synthesising each
    separately adds gaps that make Riya sound slow/choppy.
    """

    def __init__(self, *, lang_code: str, **kwargs):
        super().__init__(**kwargs)
        self._lang_code = lang_code or "hi"

    def _build_msg(self, text: str) -> dict:
        msg = super()._build_msg(text)
        msg["language"] = self._lang_code  # force a valid string (never null)
        return msg

    async def run_tts(self, text: str, context_id: str):
        if not text or _PUNCT_ONLY.match(text):
            logger.debug(f"[tts] skipping punctuation-only fragment: {text!r}")
            yield None
            return
        async for frame in super().run_tts(text, context_id):
            yield frame

    async def _send_keepalive(self):
        # Parent's keepalive also sends language from settings (may be null) — override.
        from websockets.protocol import State
        if self._websocket and self._websocket.state is State.OPEN:
            await self._websocket.send(json.dumps({
                "text": " ",
                "voice_id": self._settings.voice,
                "language": self._lang_code,
            }))


class ElevenLabsV3HttpTTSService(ElevenLabsHttpTTSService):
    """ElevenLabs HTTP TTS for the ``eleven_v3`` model.

    The stock HTTP service accumulates each spoken sentence into ``_previous_text``
    and sends it on the next request for cross-sentence continuity. eleven_v3
    rejects that with `"Providing previous_text or next_text is not yet supported
    with the 'eleven_v3' model."` — which broke every sentence after the first
    (only the greeting was heard). We simply never carry previous_text for v3.
    """

    @property
    def _previous_text(self) -> str:
        return ""

    @_previous_text.setter
    def _previous_text(self, value) -> None:  # ignore all accumulation
        pass


class CaptionsSideChannel(FrameProcessor):
    """Sends caption JSON directly over the WebSocket (text frames), bypassing
    the protobuf binary path. Browser distinguishes string vs binary messages."""

    def __init__(self, websocket: WebSocket, role: str):
        super().__init__()
        self._ws = websocket
        self._role = role  # "user" or "bot" — informational only

    async def _send(self, msg_type: str, text: str):
        try:
            await self._ws.send_text(json.dumps({"type": msg_type, "text": text}))
        except Exception:
            # Don't let caption-channel hiccups break the pipeline.
            pass

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterimTranscriptionFrame) and (frame.text or "").strip():
            await self._send("user_interim", frame.text)
        elif isinstance(frame, TranscriptionFrame) and (frame.text or "").strip():
            await self._send("user", frame.text)
        elif isinstance(frame, LLMTextFrame) and (frame.text or ""):
            await self._send("bot", frame.text)
        await self.push_frame(frame, direction)


class TranscriptionLogger(FrameProcessor):
    """Logs only finalized STT transcriptions to the server console."""

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            logger.info(f"[stt] {frame.text!r}")
        await self.push_frame(frame, direction)


# ---------------------------------------------------------------------------
# Backchannel: the little "hmm / अच्छा / हाँ हाँ" a real Indian listener makes while
# the other person is still talking. Short clips are synthesised once per
# (language, voice) with Chirp3-HD and cached; while the learner speaks we play one
# every few seconds at reduced gain. They go out as plain OutputAudioRawFrame, which
# pipecat does NOT count as bot speech, so turn-taking, captions, interruption logic
# and the STT gate are untouched. Browser echo cancellation keeps them out of the mic.
BACKCHANNEL_TEXTS = {
    # Word fillers (synthesised per language). Hums come from the recorded clips in
    # data/backchannel/custom/*.mp3 and are used for every language.
    "hin": ["अच्छा", "ओके", "ठीक है"],
    "tel": ["సరే", "ఓకే", "అవును"],
    "tam": ["சரி", "ஓகே", "ஆமா"],
    "kan": ["ಸರಿ", "ಓಕೆ", "ಹೌದು"],
    "mal": ["ശരി", "ഓകെ", "അതെ"],
    "ben": ["আচ্ছা", "ওকে", "ঠিক আছে"],
    "eng": ["okay", "right", "I see"],
}
_BACKCHANNEL_CUSTOM_DIR = Path(__file__).parent / "data" / "backchannel" / "custom"


def _load_custom_hums() -> list[bytes]:
    """Decode the recorded hum clips (mp3/wav) to PCM16 24 kHz mono, cached as .pcm."""
    clips = []
    if not _BACKCHANNEL_CUSTOM_DIR.exists():
        return clips
    for src in sorted(_BACKCHANNEL_CUSTOM_DIR.iterdir()):
        if src.suffix.lower() not in (".mp3", ".wav", ".m4a", ".ogg"):
            continue
        cache = src.with_suffix(".pcm")
        if cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime:
            clips.append(cache.read_bytes()); continue
        import av
        container = av.open(str(src))
        res = av.AudioResampler(format="s16", layout="mono", rate=OUTPUT_SAMPLE_RATE)
        pcm = bytearray()
        for frame in container.decode(container.streams.audio[0]):
            for rf in res.resample(frame):
                pcm += rf.to_ndarray().tobytes()
        pcm = _trim_silence(bytes(pcm))
        cache.write_bytes(pcm); clips.append(pcm)
    return clips
_BACKCHANNEL_DIR = Path(__file__).parent / "data" / "backchannel"


def _synth_backchannel_clips(lang_key: str, voice: str, credentials_path: str) -> tuple[list[bytes], list[bytes]]:
    """Blocking: return PCM16 24 kHz clips for every filler of the language, from disk
    cache or Chirp3-HD. Runs in a worker thread at session start."""
    from google.cloud import texttospeech as gtts
    from google.oauth2 import service_account
    code = GOOGLE_LANG_CODE.get(lang_key, "en-IN" if lang_key == "eng" else "hi-IN")
    texts = BACKCHANNEL_TEXTS.get(lang_key) or BACKCHANNEL_TEXTS["eng"]
    _BACKCHANNEL_DIR.mkdir(parents=True, exist_ok=True)
    clips, client = [], None
    for i, text in enumerate(texts):
        path = _BACKCHANNEL_DIR / f"{lang_key}_{voice}_{i}.pcm"
        if path.exists():
            clips.append(path.read_bytes()); continue
        if client is None:
            creds = service_account.Credentials.from_service_account_file(credentials_path)
            client = gtts.TextToSpeechClient(credentials=creds)
        r = client.synthesize_speech(
            input=gtts.SynthesisInput(text=text),
            voice=gtts.VoiceSelectionParams(language_code=code, name=f"{code}-Chirp3-HD-{voice}"),
            audio_config=gtts.AudioConfig(audio_encoding=gtts.AudioEncoding.LINEAR16,
                                          sample_rate_hertz=OUTPUT_SAMPLE_RATE),
        )
        pcm = r.audio_content[44:] if r.audio_content[:4] == b"RIFF" else r.audio_content
        pcm = _trim_silence(pcm)
        path.write_bytes(pcm); clips.append(pcm)
    return _load_custom_hums(), clips


def _trim_silence(pcm: bytes, thresh: float = 0.02, pad_ms: int = 60) -> bytes:
    """Cut Chirp's leading/trailing silence so a filler lands tight on the pause."""
    import numpy as np
    x = np.frombuffer(pcm, dtype=np.int16)
    loud = np.flatnonzero(np.abs(x.astype(np.int32)) > thresh * 32767)
    if loud.size == 0:
        return pcm
    pad = OUTPUT_SAMPLE_RATE * pad_ms // 1000
    a, b = max(0, loud[0] - pad), min(x.size, loud[-1] + pad)
    return x[a:b].tobytes()


class BackchannelProcessor(FrameProcessor):
    """Plays soft native fillers while the learner talks.

    Two triggers, like a real listener:
      • continuous speech: one filler every min..max seconds (random each time)
      • micro-pause: when VAD says the learner paused and they had spoken for at
        least ~min seconds since the last filler, say one right at the pause
    Never while Riya herself is speaking. Clips are synthesised once (Chirp3-HD,
    picked voice) in a worker thread and cached on disk.
    """

    POLL_SECS = 0.2

    def __init__(self, *, lang_key: str, voice: str, credentials_path: str,
                 min_secs: float, max_secs: float, gain: float):
        super().__init__()
        self._lang, self._voice, self._creds = lang_key, voice, credentials_path
        self._min, self._max = min_secs, max(max_secs, min_secs)
        self._gain = max(0.0, min(1.0, gain))
        self._clips: list[bytes] = []     # all clips (hums + words), for "any loaded" checks
        self._hums: list[bytes] = []
        self._words: list[bytes] = []
        self._last_clip: bytes | None = None
        self._load_future = None
        self._loop_task: asyncio.Task | None = None
        self._bot_speaking = False
        self._user_speaking = False
        self._since = 0.0          # monotonic: start of speech since last filler
        self._last_idx = -1

    # ---- clips -------------------------------------------------------------
    def _kick_load(self):
        if self._clips or self._load_future is not None:
            return
        loop = asyncio.get_running_loop()
        self._load_future = loop.run_in_executor(
            None, _synth_backchannel_clips, self._lang, self._voice, self._creds)

        def _done(fut):
            try:
                self._hums, self._words = fut.result()
                self._clips = self._hums + self._words
                logger.info(f"[backchannel] ready: {len(self._hums)} hums + {len(self._words)} words lang={self._lang} voice={self._voice}")
            except Exception as e:
                logger.warning(f"[backchannel] clip synthesis failed: {e}")
        self._load_future.add_done_callback(_done)

    # ---- frames ------------------------------------------------------------
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._kick_load()
            if not self._user_speaking:
                self._user_speaking = True
                if not self._since:
                    self._since = time.monotonic()
                self._start_loop()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._user_speaking = False
            self._stop_loop()
            spoken = time.monotonic() - self._since if self._since else 0.0
            if spoken >= self._min * 0.6 and not self._bot_speaking and self._clips:
                await self._play_random()   # filler right at the micro-pause
                self._since = 0.0
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._stop_loop()
            self._since = 0.0
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        await self.push_frame(frame, direction)

    def _start_loop(self):
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.get_running_loop().create_task(self._run())

    def _stop_loop(self):
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
        self._loop_task = None

    async def _run(self):
        """While the learner keeps talking, fire a filler every min..max seconds."""
        try:
            target = random.uniform(self._min, self._max)
            while self._user_speaking:
                await asyncio.sleep(self.POLL_SECS)
                if self._bot_speaking or not self._clips or not self._since:
                    continue
                if time.monotonic() - self._since >= target:
                    await self._play_random()
                    self._since = time.monotonic()
                    target = random.uniform(self._min, self._max)
        except asyncio.CancelledError:
            pass

    HUM_SHARE = 0.7   # ~7 of 10 fillers are hums, the rest a word (अच्छा / ओके / ठीक है)

    async def _play_random(self):
        pool = self._hums if (self._hums and (not self._words or random.random() < self.HUM_SHARE)) else self._words
        if not pool:
            return
        choices = [c for c in pool if c is not self._last_clip] or pool
        clip = random.choice(choices)
        self._last_clip = clip
        self._last_idx = self._clips.index(clip) if clip in self._clips else -1
        await self._play(clip)

    async def _play(self, pcm: bytes):
        import numpy as np
        data = (np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * self._gain).astype(np.int16).tobytes()
        step = OUTPUT_SAMPLE_RATE * 2 // 50  # 20 ms frames
        logger.info(f"[backchannel] play clip {self._last_idx} ({len(data) / (OUTPUT_SAMPLE_RATE * 2):.2f}s)")
        for i in range(0, len(data), step):
            await self.push_frame(OutputAudioRawFrame(audio=data[i:i + step], sample_rate=OUTPUT_SAMPLE_RATE, num_channels=1))

    async def cleanup(self):
        self._stop_loop()
        await super().cleanup()


class TranscriptionGate(FrameProcessor):
    """Word-gated turn + interruption control.

    Jab Riya BOL rahi hoti hai:
      - user >= INTERRUPT_WORDS bole  → Riya ko INTERRUPT karo aur transcript aage
        bhejo → wo reply degi.
      - kam words / noise / echo      → ignore, Riya apni baat jaari rakhe.

    Jab Riya CHUP hai (user ki baari):
      - empty/0-word  → drop.
      - 1+ word       → aage bhejo (reply).

    interrupt_words = interrupt ke liye minimum words (settings se aata hai)."""

    MIN_TURN_WORDS = 1
    ECHO_COOLDOWN_SECS = 0.8

    def __init__(self, interrupt_words: int = 2, allow_interruptions: bool = False):
        super().__init__()
        self._interrupt_words = max(1, interrupt_words)
        self._allow_interruptions = allow_interruptions
        self._bot_speaking = False
        self._bot_stopped_at = 0.0  # monotonic seconds

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._bot_stopped_at = time.monotonic()

        if isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            w = len(text.split())

            # Auto-detect hallucinates noise/echo into foreign scripts (CJK,
            # Cyrillic, …) — drop it regardless of turn state. The user only
            # speaks Indian languages + English.
            if _is_foreign_garbage(text):
                logger.info(f"[gate] dropped foreign-script garbage: {text!r}")
                return
            if _GHOST_TEXT.search(text):
                logger.info(f"[gate] dropped Whisper ghost text: {text!r}")
                return

            if self._bot_speaking:
                # Interruptions OFF → drop EVERYTHING while Riya speaks (this is what
                # kills echo: Riya's own voice coming back through the mic is ignored
                # instead of interrupting her). Only interrupt when explicitly enabled.
                if self._allow_interruptions and w >= self._interrupt_words:
                    logger.info(f"[gate] INTERRUPT ({w}w): {text!r}")
                    await self.broadcast_interruption()
                    await self.push_frame(frame, direction)
                    return
                logger.info(f"[gate] ignored while Riya speaking ({w}w): {text!r}")
                return

            recently_stopped = (
                time.monotonic() - self._bot_stopped_at < self.ECHO_COOLDOWN_SECS
            )
            if w < self.MIN_TURN_WORDS:
                why = "trailing/echo" if recently_stopped else "short noise"
                logger.info(f"[gate] dropped ({why}, {w}w): {text!r}")
                return

        await self.push_frame(frame, direction)


# Languages this bot ever needs to understand. Used to CONSTRAIN ElevenLabs
# auto-detect: unconstrained, Scribe hallucinates noise/echo into whatever
# language it guesses (Russian, Chinese, …), so real speech never survives the
# foreign-script gate. Constraining detection to just these keeps true
# multilingual input while making Russian/CJK impossible. ISO-639-3 in; ElevenLabs
# normalises on echo (hin→hi, tel→te, …).
STT_SUPPORTED_LANGS = ["hin", "tel", "tam", "kan", "mal", "ben", "eng"]
STT_PRIMARY_LANG = "hin"  # bias on ambiguous audio; greeting is Hindi-primary


def _multilang_language_value(primary: str = STT_PRIMARY_LANG,
                              secondary: list[str] | None = None) -> str:
    """Compose the `language_code` value pipecat drops verbatim into the realtime
    URL (it builds `language_code={value}` with no escaping — see pipecat
    elevenlabs/stt.py `_connect_websocket`). We piggyback ElevenLabs'
    `secondary_languages` onto it as REPEATED query params.

    `primary` is the expected language. `secondary` is the fallback set; a language
    NOT in {primary} ∪ secondary simply cannot be produced. This matters: primary
    is only a HINT, not a lock, so any Indian language left in `secondary` can still
    capture (and mis-script) the primary's audio — e.g. with tel primary + hin
    secondary, spoken Telugu leaks out as Devanagari. So when a learner picks a
    language we pass secondary=["eng"] only: Telugu audio → Telugu, English words →
    English, and Hindi/Tamil/… are absent so they can't hijack the transcript.
    Default (no pick) keeps the full set since the language is unknown.

    A live probe against the India residency endpoint confirmed:
      • repeated form → language_code=<primary>, secondary_languages=[…] ✓
      • comma-joined form → BOTH params silently reset to auto-detect ✗ (never use)
    Result e.g. (primary=tel, secondary=[eng]): 'tel&secondary_languages=eng'
    """
    if primary not in STT_SUPPORTED_LANGS:
        primary = STT_PRIMARY_LANG
    if secondary is None:
        secondary = [l for l in STT_SUPPORTED_LANGS if l != primary]
    else:
        secondary = [l for l in secondary if l in STT_SUPPORTED_LANGS and l != primary]
    tail = "".join(f"&secondary_languages={l}" for l in secondary)
    return f"{primary}{tail}"


def _build_stt(cfg: dict, http_base: str, stt_host: str,
               aiohttp_session: aiohttp.ClientSession | None):
    """ElevenLabs Scribe — realtime WebSocket (default) or batch."""
    model = cfg["ELEVENLABS_STT_MODEL"]
    lang = (cfg["ELEVENLABS_STT_LANGUAGE"] or "").strip().lower()
    # "auto" (or blank) → constrained multilingual; anything else hard-locks it.
    lang = None if lang in ("", "auto") else lang
    api_key = os.getenv("ELEVENLABS_API_KEY")

    if model == "scribe_v2":
        # Batch (segmented) STT — higher accuracy, more latency. Needs an HTTP session.
        return ElevenLabsSTTService(
            api_key=api_key,
            aiohttp_session=aiohttp_session,
            base_url=http_base,
            sample_rate=INPUT_SAMPLE_RATE,
            settings=ElevenLabsSTTService.Settings(model=model),
        )

    # Realtime Scribe (recommended). commit_strategy=manual → local Silero VAD
    # controls when transcript segments commit (consistent with the pipeline VAD).
    commit = (
        CommitStrategy.VAD
        if cfg["ELEVENLABS_STT_COMMIT_STRATEGY"] == "vad"
        else CommitStrategy.MANUAL
    )
    # IMPORTANT: language MUST go in Settings, not params. When `settings=` is
    # passed, pipecat skips the params-derived language entirely (`if not
    # settings:`), so a params language_code silently becomes None → auto-detect
    # → Telugu gets mis-transcribed as Hindi. Setting it on Settings makes it
    # land in the realtime URL as language_code=<lang>.
    stt_settings = ElevenLabsRealtimeSTTService.Settings(model=model)
    # IMPORTANT: we do NOT force the picked language as the STT primary. Empirically
    # (see logs 2026-07-21), forcing Telugu primary makes ElevenLabs ROMANISE spoken
    # Telugu ("Namaskaram" instead of నమస్కారం), and forcing it with Hindi in the
    # secondary set leaks Telugu out as Devanagari. The plain constrained-multilingual
    # set (Hindi primary + all Indian langs + English as secondaries) is what actually
    # transcribes spoken Telugu as proper Telugu script. So the picker drives only
    # Riya's opening LANGUAGE (in run_bot); STT stays on the multilingual set.
    if lang:
        # Explicit hard lock saved in Settings → transcribe that one language only.
        stt_settings.language = lang
        logger.info(f"[stt] hard-locked to '{lang}' (settings)")
    else:
        # Settings=auto → constrained multilingual (all supported Indian langs +
        # English), NOT free auto-detect (which hallucinates Russian/CJK on noise).
        stt_settings.language = _multilang_language_value()
        logger.info(f"[stt] constrained multilingual, primary='{STT_PRIMARY_LANG}' (works for Telugu)")
    return ElevenLabsRealtimeSTTService(
        api_key=api_key,
        base_url=stt_host,
        commit_strategy=commit,
        sample_rate=INPUT_SAMPLE_RATE,
        settings=stt_settings,
    )


class MeteredGroqSTTService(GroqSTTService):
    """Groq Whisper with (1) real auto-detect — the stock _transcribe asserts a
    language is set, so language=None would error on every turn — and (2) each
    segment's audio length reported to the call's CostMeter (Groq bills every
    request at a 10 s minimum)."""

    def __init__(self, *, meter: "CostMeter", **kwargs):
        super().__init__(**kwargs)
        self._meter = meter

    async def run_stt(self, audio: bytes):
        # Segmented STT hands us a mono 16-bit WAV (44-byte header).
        rate = self.sample_rate or INPUT_SAMPLE_RATE
        self._meter.add_stt_segment(max(0, len(audio) - 44) / (2 * rate))
        async for frame in super().run_stt(audio):
            yield frame

    async def _transcribe(self, audio: bytes):
        kwargs = {
            "file": ("audio.wav", audio, "audio/wav"),
            "model": self._settings.model,
            "response_format": "verbose_json" if self._include_prob_metrics else "json",
        }
        if self._settings.language:
            kwargs["language"] = self._settings.language
        return await self._client.audio.transcriptions.create(**kwargs)


def _build_groq_stt(cfg: dict, lang_key: str, meter: "CostMeter"):
    """Groq Whisper, segmented on our Silero VAD turns (no realtime WebSocket).

    Language: a GROQ_STT_LANGUAGE lock wins, else the call-page pick, else Whisper
    auto-detect (TranscriptionGate drops foreign-script hallucinations)."""
    lock = cfg["GROQ_STT_LANGUAGE"]
    language = GOOGLE_LANG.get(lock if lock != "auto" else lang_key)
    logger.info(f"[stt] Groq {cfg['GROQ_STT_MODEL']} language="
                f"{language.value if language else 'auto-detect'}")
    return MeteredGroqSTTService(
        meter=meter,
        api_key=os.getenv("GROQ_API_KEY"),
        sample_rate=INPUT_SAMPLE_RATE,
        settings=GroqSTTService.Settings(model=cfg["GROQ_STT_MODEL"], language=language),
    )


# Quote marks at a word boundary (not the apostrophe inside "I'm" / "don't").
_BOUNDARY_QUOTES = re.compile(r"(?<!\w)['‘’\"“”]|['‘’\"“”](?!\w)")


class SarvamSkipPunctTTSService(SarvamTTSService):
    """Sarvam Bulbul with quote-safe input. Sarvam's WebSocket re-chunks text on its
    side, so `बोलिए: 'I go to the office.'` leaves a trailing "'" chunk that it
    rejects ("Text must contain at least one character from the allowed
    languages") — one ErrorFrame per quoted line. Quotes aren't spoken, so strip
    them at word boundaries and skip anything left that is punctuation only."""

    async def run_tts(self, text: str, context_id: str):
        text = _BOUNDARY_QUOTES.sub("", text or "")
        if not text or _PUNCT_ONLY.match(text):
            logger.debug(f"[tts] skipping punctuation-only fragment: {text!r}")
            yield None
            return
        async for frame in super().run_tts(text, context_id):
            yield frame


def _build_sarvam_tts(cfg: dict, voice_id: str, speed: float, lang_key: str):
    """Sarvam Bulbul over the streaming WebSocket.

    Bulbul has no per-voice locale, so target_language_code comes from the call's
    picked language (Hindi when none is picked). The picked voice wins over
    SARVAM_TTS_VOICE, but only if it is a speaker of the selected model — a stale
    name (e.g. the retired v2 "anushka") would fail the call.
    """
    model = cfg["SARVAM_TTS_MODEL"]
    speakers = settings_store.sarvam_speakers(model)
    candidates = [(voice_id or "").strip(), cfg["SARVAM_TTS_VOICE"].strip()]
    voice = next((v for v in candidates if v in speakers), speakers[0])
    if voice_id and voice_id != voice:
        logger.warning(f"[tts] {voice_id!r} is not a {model} speaker — using {voice!r}")
    # Per-call slider speed wins; fall back to the saved Sarvam pace.
    pace = speed if speed and speed != 1.0 else cfg["SARVAM_TTS_PACE"]
    language = Language.EN_IN if lang_key == "eng" else GOOGLE_LANG.get(lang_key, Language.HI_IN)
    logger.info(f"[tts] Sarvam {model} voice={voice} lang={language.value} pace={pace} "
                f"min_buffer={cfg['SARVAM_TTS_MIN_BUFFER']}")
    return SarvamSkipPunctTTSService(
        api_key=os.getenv("SARVAM_API_KEY"),
        sample_rate=OUTPUT_SAMPLE_RATE,
        settings=SarvamTTSService.Settings(
            model=model,
            voice=voice,
            language=language,
            pace=pace,
            temperature=cfg["SARVAM_TTS_TEMPERATURE"],
            enable_preprocessing=cfg["SARVAM_TTS_PREPROCESSING"],
            min_buffer_size=cfg["SARVAM_TTS_MIN_BUFFER"],
        ),
    )


class CostMeter(BaseObserver):
    """Per-call cost + latency meter — turns "~₹0.80/min" into a measurement.

    TTS characters and Gemini tokens come from pipecat usage MetricsFrames, STT
    audio seconds from MeteredGroqSTTService, user→bot latency from
    UserBotLatencyObserver. Priced with the "Cost meter" settings. Providers
    without a rate show as n/a. After each bot turn a {"type": "cost"} message
    goes out on the caption side channel.
    """

    def __init__(self, cfg: dict, websocket: WebSocket):
        super().__init__()
        self._cfg = cfg
        self._ws = websocket
        self._started = time.monotonic()
        self._seen_metrics: set[int] = set()  # observers see a frame once per hop
        self._last_sent = None
        self.tts_chars = 0
        self.tts_tok_in = 0     # Gemini TTS: text tokens in
        self.tts_tok_out = 0    # Gemini TTS: audio tokens out (what is billed)
        self.llm_in = 0
        self.llm_out = 0
        self.stt_requests = 0
        self.stt_secs = 0.0
        self.stt_billed_secs = 0.0
        self.latencies: list[float] = []

    def add_tts_usage(self, tok_in: int, tok_out: int):
        self.tts_tok_in += tok_in
        self.tts_tok_out += tok_out

    def add_stt_segment(self, secs: float):
        self.stt_requests += 1
        self.stt_secs += secs
        self.stt_billed_secs += max(secs, self._cfg["RATE_GROQ_MIN_BILLED_SECS"])

    def add_latency(self, secs: float):
        self.latencies.append(secs)

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if isinstance(frame, MetricsFrame):
            if frame.id in self._seen_metrics:
                return
            self._seen_metrics.add(frame.id)
            for d in frame.data:
                if isinstance(d, TTSUsageMetricsData):
                    self.tts_chars += d.value
                elif isinstance(d, LLMUsageMetricsData):
                    self.llm_in += d.value.prompt_tokens
                    self.llm_out += d.value.completion_tokens
                elif isinstance(d, TTFBMetricsData) and d.value > 0:
                    logger.info(f"[ttfb] {d.processor} {d.value:.2f}s")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            await self._send()

    def summary(self) -> dict:
        c = self._cfg
        usd_inr = c["USD_INR"]
        minutes = max((time.monotonic() - self._started) / 60, 1 / 60)
        tts = stt = None
        if c["TTS_PROVIDER"] == "sarvam":
            tts = self.tts_chars / 10_000 * c["RATE_SARVAM_TTS_INR_PER_10K_CHARS"]
        elif c["TTS_PROVIDER"] == "google":
            tts = self.tts_chars / 1e6 * c["RATE_GOOGLE_CHIRP_USD_PER_1M_CHARS"] * usd_inr
        elif c["TTS_PROVIDER"] in ("gemini_lite", "gemini_flash"):
            audio_rate = (c["RATE_GEMINI_LITE_TTS_USD_PER_1M_AUDIO_TOK"] if c["TTS_PROVIDER"] == "gemini_lite"
                          else c["RATE_GEMINI_FLASH_TTS_USD_PER_1M_AUDIO_TOK"])
            tts = (self.tts_tok_out * audio_rate + self.tts_tok_in * c["RATE_GEMINI_TTS_USD_PER_1M_TEXT_TOK"]) / 1e6 * usd_inr
        if c["STT_PROVIDER"] == "groq":
            rate = (c["RATE_GROQ_LARGE_USD_PER_HOUR"] if c["GROQ_STT_MODEL"] == "whisper-large-v3"
                    else c["RATE_GROQ_TURBO_USD_PER_HOUR"])
            stt = self.stt_billed_secs / 3600 * rate * usd_inr
        elif c["STT_PROVIDER"] == "sarvam":
            # Streaming STT receives the mic audio for the whole call → call length.
            stt = minutes / 60 * c["RATE_SARVAM_STT_INR_PER_HOUR"]
        if c["LLM_PROVIDER"] == "groq":
            rin, rout = c["RATE_GROQ_LLM_USD_IN_PER_1M"], c["RATE_GROQ_LLM_USD_OUT_PER_1M"]
        else:
            rin, rout = c["RATE_LLM_USD_IN_PER_1M"], c["RATE_LLM_USD_OUT_PER_1M"]
        llm = (self.llm_in * rin + self.llm_out * rout) / 1e6 * usd_inr
        total = llm + (tts or 0) + (stt or 0)
        lat = sorted(self.latencies)
        p50 = lat[len(lat) // 2] if lat else None
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None

        def inr(v):
            return "n/a" if v is None else f"₹{v:.2f}"

        text = (f"{inr(total)} (tts {inr(tts)} · stt {inr(stt)} · llm {inr(llm)}) · "
                f"{minutes:.1f} min · ₹{total / minutes:.2f}/min · "
                f"{self.tts_chars} chars"
                + (f" ({self.tts_tok_out} audio tok)" if self.tts_tok_out else "")
                + f" · {self.stt_requests} stt req "
                f"({self.stt_secs:.0f}s audio, {self.stt_billed_secs:.0f}s billed) · "
                f"{self.llm_in}/{self.llm_out} tok")
        if p50 is not None:
            text += f" · latency p50 {p50:.2f}s p95 {p95:.2f}s (n={len(lat)})"
        return {
            "text": text, "total_inr": total, "tts_inr": tts, "stt_inr": stt, "llm_inr": llm,
            "minutes": minutes, "inr_per_min": total / minutes,
            "latency_p50": p50, "latency_p95": p95,
        }

    async def _send(self):
        # BotStoppedSpeakingFrame travels both directions — send only on change.
        key = (self.tts_chars, self.llm_in, self.llm_out, self.stt_requests, len(self.latencies))
        if key == self._last_sent:
            return
        self._last_sent = key
        s = self.summary()
        logger.info(f"[cost] {s['text']}")
        try:
            await self._ws.send_text(json.dumps({"type": "cost", **s}))
        except Exception:
            pass


def _build_smallest_tts(cfg: dict, speed: float, picked_voice: str = ""):
    """Smallest.ai Waves — native WebSocket streaming, low latency, Indian languages.

    The voice picked on the test page wins; SMALLEST_VOICE is only the fallback.
    """
    voice = (picked_voice or cfg["SMALLEST_VOICE"] or "anitha").strip()
    lang = (cfg["SMALLEST_LANGUAGE"] or "hi").strip()
    model = cfg["SMALLEST_MODEL"]
    # Per-call slider speed wins; fall back to the saved Smallest speed.
    spd = speed if speed and speed != 1.0 else cfg["SMALLEST_SPEED"]
    logger.info(f"[tts] Smallest.ai {model} voice={voice} lang={lang} speed={spd}")
    return SmallestFixedLangTTSService(
        api_key=os.getenv("SMALLEST_API_KEY"),
        lang_code=lang,
        sample_rate=OUTPUT_SAMPLE_RATE,
        settings=SmallestTTSService.Settings(
            model=model,
            voice=voice,
            speed=spd,
        ),
    )


def _build_tts(cfg: dict, voice_id: str, speed: float,
               http_base: str, ws_stream_url: str,
               aiohttp_session: aiohttp.ClientSession | None):
    """TTS factory. Smallest.ai (WebSocket) or ElevenLabs (streaming / v3 HTTP).

    For Smallest, voice_id is the picked Smallest voice (e.g. "anitha"); for
    ElevenLabs it is the picked ElevenLabs voice id.
    """
    if cfg["TTS_PROVIDER"] == "smallest":
        return _build_smallest_tts(cfg, speed, picked_voice=voice_id)

    model = cfg["ELEVENLABS_TTS_MODEL"]
    lang = (cfg["ELEVENLABS_TTS_LANGUAGE"] or "").strip() or None
    api_key = os.getenv("ELEVENLABS_API_KEY")

    voice_kwargs = dict(
        voice=voice_id,
        model=model,
        speed=speed,
        stability=cfg["ELEVENLABS_TTS_STABILITY"],
        similarity_boost=cfg["ELEVENLABS_TTS_SIMILARITY_BOOST"],
        style=cfg["ELEVENLABS_TTS_STYLE"],
        use_speaker_boost=cfg["ELEVENLABS_TTS_USE_SPEAKER_BOOST"],
        apply_text_normalization=cfg["ELEVENLABS_TTS_APPLY_TEXT_NORMALIZATION"],
    )
    if lang:
        voice_kwargs["language"] = lang

    if model == "eleven_v3":
        # eleven_v3 is not on the streaming WebSocket — use HTTP. The V3 subclass
        # suppresses previous_text so multi-sentence replies don't 400.
        logger.info("[tts] eleven_v3 selected → HTTP path (natural Telugu + emotion tags, higher latency)")
        return ElevenLabsV3HttpTTSService(
            api_key=api_key,
            base_url=http_base,
            aiohttp_session=aiohttp_session,
            sample_rate=OUTPUT_SAMPLE_RATE,
            settings=ElevenLabsHttpTTSService.Settings(**voice_kwargs),
        )

    return ElevenLabsTTSService(
        api_key=api_key,
        url=ws_stream_url,
        sample_rate=OUTPUT_SAMPLE_RATE,
        settings=ElevenLabsTTSSettings(**voice_kwargs),
    )


# When the learner picks a language up-front, we skip Riya's "which language are
# you comfortable in?" question by seeding the conversation with a first user turn
# ALREADY in that language. Riya then greets and continues directly in it. Each
# phrase means roughly "Namaste, let's talk in <language>." — kept in the native
# script so the model locks onto the language from turn one. This seed turn is
# context-only: it never passes through the caption side-channel, so the learner
# never sees it. No/unknown language → "." (Riya asks as before).
LANG_KICKOFF = {
    "hin": "नमस्ते, चलिए हिंदी में बात करते हैं।",
    "tel": "నమస్కారం, తెలుగులో మాట్లాడదాం.",
    "tam": "வணக்கம், தமிழில் பேசலாம்.",
    "kan": "ನಮಸ್ಕಾರ, ಕನ್ನಡದಲ್ಲಿ ಮಾತನಾಡೋಣ.",
    "mal": "നമസ്കാരം, മലയാളത്തിൽ സംസാരിക്കാം.",
    "ben": "নমস্কার, চলুন বাংলায় কথা বলি।",
    "eng": "Hello, let's talk in English.",
}

# Human-readable names for the system-prompt directive that pins the opening
# language, so Riya reliably starts in the picked language instead of drifting to
# English (a bare seed turn drifts at higher temperatures).
LANG_NAMES = {
    "hin": "Hindi", "tel": "Telugu", "tam": "Tamil",
    "kan": "Kannada", "mal": "Malayalam", "ben": "Bengali", "eng": "English",
}


# Production-style per-language prompt blocks. Prompts written for production carry a
# `{language_block}` placeholder; we fill it from data/language_blocks/call_<xx>.txt
# (copied from sahi-english apps/prompts/language_blocks.py) so the same prompt text
# behaves here as it does in the app. Unmapped/absent language -> English block, exactly
# like production's LANGUAGE_BLOCK_EN fallback.
_LANGUAGE_BLOCK_DIR = Path(__file__).parent / "data" / "language_blocks"
_LANGUAGE_BLOCK_FILE = {"hin": "hi", "tel": "te", "tam": "ta", "kan": "kn"}


def _apply_language_block(prompt: str, lang_key: str) -> str:
    if "{language_block}" not in prompt:
        return prompt
    code = _LANGUAGE_BLOCK_FILE.get(lang_key, "en")
    path = _LANGUAGE_BLOCK_DIR / f"call_{code}.txt"
    if not path.exists():
        path = _LANGUAGE_BLOCK_DIR / "call_en.txt"
    block = path.read_text().strip()
    logger.info(f"[prompt] language_block <- {path.name}")
    return prompt.replace("{language_block}", block)


def _language_directive(lang_key: str) -> str:
    """A short instruction appended to the system prompt when the learner has
    pre-picked a language, forcing Riya to open and continue in it."""
    name = LANG_NAMES.get(lang_key)
    if not name:
        return ""
    return (
        f"\n\n[SESSION LANGUAGE] The learner has chosen to talk in {name}. "
        f"Greet them and run the ENTIRE session in {name}. Do NOT ask which "
        f"language they are comfortable in — that is already decided. You may still "
        f"mix in English words where natural for teaching English."
    )


async def run_bot(
    websocket: WebSocket,
    system_prompt: str,
    voice_id: str,
    *,
    speed: float = 1.0,
    temperature: float | None = None,
    language: str | None = None,
) -> None:
    cfg = settings_store.get_all()
    http_base, ws_stream_url, stt_host = settings_store.elevenlabs_urls(cfg)

    # Per-call slider overrides fall back to the saved settings.
    if temperature is None:
        temperature = cfg["GEMINI_TEMPERATURE"]

    vad_params = VADParams(
        confidence=cfg["VAD_CONFIDENCE"],
        start_secs=cfg["VAD_START_SECS"],
        stop_secs=cfg["VAD_STOP_SECS"],
        min_volume=cfg["VAD_MIN_VOLUME"],
    )
    vad_analyzer = SileroVADAnalyzer(params=vad_params)
    logger.info(f"[bot] VAD: {vad_analyzer.params!r}")
    logger.info(
        f"[bot] LLM={cfg['GEMINI_MODEL']} temp={temperature} | "
        f"STT={cfg['STT_PROVIDER']} | TTS={cfg['TTS_PROVIDER']} speed={speed}"
    )
    meter = CostMeter(cfg, websocket)

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=INPUT_SAMPLE_RATE,
            audio_out_sample_rate=OUTPUT_SAMPLE_RATE,
            add_wav_header=False,
            vad_analyzer=vad_analyzer,
            serializer=ProtobufFrameSerializer(),
        ),
    )

    # An aiohttp session is needed by the ElevenLabs HTTP TTS path (eleven_v3), the
    # batch STT path (scribe_v2), and the Gemini STT/TTS services; create one only
    # when a selected model needs it.
    needs_http = (
        (cfg["TTS_PROVIDER"] == "elevenlabs" and cfg["ELEVENLABS_TTS_MODEL"] == "eleven_v3")
        or cfg["ELEVENLABS_STT_MODEL"] == "scribe_v2"
    )
    http_session = aiohttp.ClientSession() if needs_http else None

    try:
        gkey = os.getenv("GOOGLE_API_KEY")
        gcreds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "gcp-service-account.json"
        lang_key = (language or "").strip().lower()

        # STT: Sarvam saaras (Indian codemix), Google Cloud (realtime), or ElevenLabs Scribe.
        if cfg["STT_PROVIDER"] == "sarvam":
            stt = SarvamSTTService(
                api_key=os.getenv("SARVAM_API_KEY"),
                model=cfg["SARVAM_STT_MODEL"], mode=cfg["SARVAM_STT_MODE"],
                sample_rate=INPUT_SAMPLE_RATE,
            )
            logger.info(f"[stt] Sarvam {cfg['SARVAM_STT_MODEL']} mode={cfg['SARVAM_STT_MODE']}")
        elif cfg["STT_PROVIDER"] == "google":
            primary = GOOGLE_LANG.get(lang_key, Language.EN_US)
            langs = [primary] if primary == Language.EN_US else [primary, Language.EN_US]
            # chirp_2 is only served from regional endpoints (verified: us-central1);
            # latest_long lives on global. Pick the location from the model.
            gmodel = cfg["GOOGLE_STT_MODEL"]
            stt = GoogleSTTService(
                credentials_path=gcreds,
                location="us-central1" if "chirp" in gmodel else "global",
                params=GoogleSTTService.InputParams(languages=langs, model=gmodel),
                sample_rate=INPUT_SAMPLE_RATE,
            )
            logger.info(f"[stt] Google Cloud STT langs={[l.value for l in langs]} model={cfg['GOOGLE_STT_MODEL']}")
        elif cfg["STT_PROVIDER"] == "groq":
            stt = _build_groq_stt(cfg, lang_key, meter)
        else:
            stt = _build_stt(cfg, http_base, stt_host, http_session)

        # TTS: Google Cloud Chirp3-HD or the ElevenLabs factory.
        if cfg["TTS_PROVIDER"] == "google":
            # Chirp3-HD voice id = "<lang>-Chirp3-HD-<VoiceName>"; the picked voice
            # (a Chirp/Gemini name like "Aoede") wins, else GOOGLE_TTS_VOICE.
            vname = (voice_id or "").strip() or cfg["GOOGLE_TTS_VOICE"]
            code = GOOGLE_LANG_CODE.get(lang_key, "en-US")
            gvoice_id = f"{code}-Chirp3-HD-{vname}"
            tts = GoogleChirpTTSService(
                credentials_path=gcreds,
                voice_id=gvoice_id,
                params=GoogleTTSService.InputParams(language=GOOGLE_LANG.get(lang_key, Language.EN_US)),
                sample_rate=OUTPUT_SAMPLE_RATE,
            )
            logger.info(f"[tts] Google Cloud TTS voice={gvoice_id}")
        elif cfg["TTS_PROVIDER"] in ("gemini_lite", "gemini_flash", "gemini"):
            gvoice = (voice_id or "").strip() or cfg["GEMINI_TTS_VOICE"]
            if cfg["TTS_PROVIDER"] == "gemini":
                gmodel_tts = cfg["GEMINI_TTS_MODEL"]  # 3.1 preview, generateContent path
                tts = GeminiFlashTTSService(
                    api_key=os.getenv("GOOGLE_API_KEY"), aiohttp_session=http_session,
                    model=gmodel_tts, voice=gvoice, style_prompt=cfg["GEMINI_TTS_STYLE"],
                    sample_rate=OUTPUT_SAMPLE_RATE,
                )
            else:
                gmodel_tts = ("gemini-3.8-flash-lite-tts" if cfg["TTS_PROVIDER"] == "gemini_lite"
                              else "gemini-3.8-flash-tts")
                tts = GeminiInteractionsTTSService(
                    api_key=os.getenv("GOOGLE_API_KEY"), aiohttp_session=http_session,
                    model=gmodel_tts, voice=gvoice, style_prompt=cfg["GEMINI_TTS_STYLE"],
                    chunking=cfg["GEMINI_TTS_CHUNKING"], sample_rate=OUTPUT_SAMPLE_RATE,
                    on_usage=meter.add_tts_usage,
                )
            logger.info(f"[tts] Gemini TTS model={gmodel_tts} voice={gvoice} chunking={cfg['GEMINI_TTS_CHUNKING']}")
        elif cfg["TTS_PROVIDER"] == "sarvam":
            tts = _build_sarvam_tts(cfg, voice_id, speed, lang_key)
        else:
            tts = _build_tts(cfg, voice_id, speed, http_base, ws_stream_url, http_session)

        # If the learner pre-picked a language, pin it in the system prompt so Riya
        # reliably opens and stays in it (a seed turn alone drifts to English at
        # higher temperatures).
        effective_prompt = _apply_language_block(system_prompt, lang_key) + _language_directive(lang_key)

        backchannel = []
        if cfg["BACKCHANNEL_ENABLED"]:
            bc_voice = (voice_id or "").strip()
            if not bc_voice or not bc_voice[0].isupper() or len(bc_voice) > 16:
                bc_voice = cfg["GOOGLE_TTS_VOICE"]  # non-Chirp voice id (ElevenLabs/Sarvam) -> default persona
            backchannel = [BackchannelProcessor(
                lang_key=lang_key, voice=bc_voice, credentials_path=gcreds,
                min_secs=cfg["BACKCHANNEL_MIN_SECS"], max_secs=cfg["BACKCHANNEL_MAX_SECS"],
                gain=cfg["BACKCHANNEL_GAIN"],
            )]
            logger.info(f"[backchannel] on: lang={lang_key} voice={bc_voice} every {cfg['BACKCHANNEL_MIN_SECS']}-{cfg['BACKCHANNEL_MAX_SECS']}s gain={cfg['BACKCHANNEL_GAIN']}")

        if cfg["LLM_PROVIDER"] == "groq":
            llm = GroqLLMService(
                api_key=os.getenv("GROQ_API_KEY"),
                settings=GroqLLMSettings(
                    model=cfg["GROQ_LLM_MODEL"],
                    system_instruction=effective_prompt,
                    temperature=temperature,
                    max_tokens=cfg["GEMINI_MAX_TOKENS"],
                    # gpt-oss are reasoning models: cap the hidden thinking or every
                    # reply costs hundreds of tokens and ~1 s extra before the first word.
                    extra=({"reasoning_effort": cfg["GROQ_REASONING_EFFORT"]}
                           if "gpt-oss" in cfg["GROQ_LLM_MODEL"] else {}),
                ),
            )
            logger.info(f"[llm] Groq model={cfg['GROQ_LLM_MODEL']} temp={temperature}")
        else:
            llm = GoogleLLMService(
                api_key=os.getenv("GOOGLE_API_KEY"),
                settings=GoogleLLMSettings(
                    model=cfg["GEMINI_MODEL"],
                    system_instruction=effective_prompt,
                    temperature=temperature,
                    max_tokens=cfg["GEMINI_MAX_TOKENS"],
                ),
            )
            logger.info(f"[llm] Gemini model={cfg['GEMINI_MODEL']} temp={temperature}")

        # Seed the first user turn. If a language was chosen on the client, seed it
        # in that language so Riya starts there directly instead of asking; otherwise
        # a bare "." just prompts her opening greeting + language question.
        kickoff = LANG_KICKOFF.get(lang_key, ".")
        if kickoff != ".":
            logger.info(f"[bot] language pre-selected: {lang_key} → seeding kickoff + system directive")
        context = LLMContext(messages=[{"role": "user", "content": kickoff}])

        allow_interruptions = bool(cfg["ALLOW_INTERRUPTIONS"])
        turn_strategies = UserTurnStrategies(
            start=[
                VADUserTurnStartStrategy(enable_interruptions=allow_interruptions),
                TranscriptionUserTurnStartStrategy(
                    use_interim=False, enable_interruptions=allow_interruptions
                ),
            ],
            stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)],
        )
        context_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                user_turn_strategies=turn_strategies,
                user_turn_stop_timeout=2.5,
            ),
        )

        vad_processor = VADProcessor(vad_analyzer=vad_analyzer)

        captions_user = CaptionsSideChannel(websocket, role="user")
        captions_bot = CaptionsSideChannel(websocket, role="bot")

        pipeline = Pipeline([
            transport.input(),
            vad_processor,
            stt,
            TranscriptionGate(
                interrupt_words=cfg["INTERRUPT_WORDS"],
                allow_interruptions=allow_interruptions,
            ),
            TranscriptionLogger(),
            captions_user,
            context_aggregator.user(),
            llm,
            captions_bot,
            tts,
            *backchannel,
            transport.output(),
            context_aggregator.assistant(),
        ])

        latency_observer = UserBotLatencyObserver()

        @latency_observer.event_handler("on_latency_measured")
        async def on_latency_measured(observer, latency, *args):
            meter.add_latency(latency)
            logger.info(f"[latency] user turn end → Riya audio {latency:.2f}s")

        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                allow_interruptions=allow_interruptions,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            observers=[meter, latency_observer],
        )

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info(f"Client connected (voice_id={voice_id}, speed={speed}, temp={temperature})")
            await task.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info("Client disconnected — ending pipeline")
            await task.cancel()

        runner = PipelineRunner(handle_sigint=False)
        await runner.run(task)
    finally:
        logger.info(f"[cost] call total: {meter.summary()['text']}")
        if http_session is not None:
            await http_session.close()
