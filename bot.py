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

import json
import os
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
    InterimTranscriptionFrame,
    LLMRunFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
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
from gemini_audio import GeminiFlashSTTService, GeminiFlashTTSService

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
STT_SUPPORTED_LANGS = ["hin", "tel", "tam", "kan", "mal", "eng"]
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
    "eng": "Hello, let's talk in English.",
}

# Human-readable names for the system-prompt directive that pins the opening
# language, so Riya reliably starts in the picked language instead of drifting to
# English (a bare seed turn drifts at higher temperatures).
LANG_NAMES = {
    "hin": "Hindi", "tel": "Telugu", "tam": "Tamil",
    "kan": "Kannada", "mal": "Malayalam", "eng": "English",
}


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
    tts_desc = (
        f"smallest:{cfg['SMALLEST_MODEL']}/{cfg['SMALLEST_VOICE']}/{cfg['SMALLEST_LANGUAGE']}"
        if cfg["TTS_PROVIDER"] == "smallest"
        else f"elevenlabs:{cfg['ELEVENLABS_TTS_MODEL']}"
    )
    logger.info(
        f"[bot] LLM={cfg['GEMINI_MODEL']} temp={temperature} | "
        f"STT={cfg['ELEVENLABS_STT_MODEL']} | TTS={tts_desc} "
        f"speed={speed} region={cfg['ELEVENLABS_REGION']}"
    )

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
        or cfg["TTS_PROVIDER"] == "gemini"
        or cfg["STT_PROVIDER"] == "gemini"
    )
    http_session = aiohttp.ClientSession() if needs_http else None

    try:
        gkey = os.getenv("GOOGLE_API_KEY")

        # STT: Gemini 3.1 Flash (batch per turn) or ElevenLabs Scribe (realtime).
        if cfg["STT_PROVIDER"] == "gemini":
            stt = GeminiFlashSTTService(
                api_key=gkey, aiohttp_session=http_session,
                model=cfg["GEMINI_STT_MODEL"], sample_rate=INPUT_SAMPLE_RATE,
            )
            logger.info(f"[stt] Gemini {cfg['GEMINI_STT_MODEL']} (batch per turn)")
        else:
            stt = _build_stt(cfg, http_base, stt_host, http_session)

        # TTS: Gemini 3.1 Flash TTS or the ElevenLabs/Smallest factory.
        if cfg["TTS_PROVIDER"] == "gemini":
            tts = GeminiFlashTTSService(
                api_key=gkey, aiohttp_session=http_session,
                model=cfg["GEMINI_TTS_MODEL"], voice=cfg["GEMINI_TTS_VOICE"],
                style_prompt=cfg["GEMINI_TTS_STYLE"], sample_rate=OUTPUT_SAMPLE_RATE,
            )
            logger.info(f"[tts] Gemini {cfg['GEMINI_TTS_MODEL']} voice={cfg['GEMINI_TTS_VOICE']}")
        else:
            tts = _build_tts(cfg, voice_id, speed, http_base, ws_stream_url, http_session)

        # If the learner pre-picked a language, pin it in the system prompt so Riya
        # reliably opens and stays in it (a seed turn alone drifts to English at
        # higher temperatures).
        lang_key = (language or "").strip().lower()
        effective_prompt = system_prompt + _language_directive(lang_key)

        llm = GoogleLLMService(
            api_key=os.getenv("GOOGLE_API_KEY"),
            settings=GoogleLLMSettings(
                model=cfg["GEMINI_MODEL"],
                system_instruction=effective_prompt,
                temperature=temperature,
                max_tokens=cfg["GEMINI_MAX_TOKENS"],
            ),
        )

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
            transport.output(),
            context_aggregator.assistant(),
        ])

        task = PipelineTask(
            pipeline,
            params=PipelineParams(allow_interruptions=allow_interruptions),
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
        if http_session is not None:
            await http_session.close()
