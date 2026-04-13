"""Voice message transcription via faster-whisper."""

import asyncio
import os
import tempfile
import logging

logger = logging.getLogger(__name__)

_model = None
_MODEL_SIZE = "small"
_transcribe_sem = asyncio.Semaphore(1)  # one transcription at a time globally


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        logger.info("Loading Whisper model '%s'...", _MODEL_SIZE)
        _model = WhisperModel(_MODEL_SIZE, device="cpu", compute_type="int8")
        logger.info("Whisper model loaded.")
    return _model


def _transcribe_sync(audio_bytes: bytes, suffix: str = ".ogg") -> str:
    """Blocking transcription — runs in thread pool. Accepts any ffmpeg-compatible format."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name
    try:
        model = _get_model()
        segments, _ = model.transcribe(tmp_path, language="ru")
        return " ".join(seg.text.strip() for seg in segments).strip()
    finally:
        os.unlink(tmp_path)


async def transcribe_ogg(ogg_bytes: bytes) -> str:
    """Transcribe OGG audio bytes to text. Non-blocking, globally serialized."""
    loop = asyncio.get_event_loop()
    async with _transcribe_sem:
        return await loop.run_in_executor(None, _transcribe_sync, ogg_bytes)


async def transcribe_audio(audio_bytes: bytes, suffix: str = ".webm") -> str:
    """Transcribe any ffmpeg-supported audio format. Non-blocking, globally serialized."""
    loop = asyncio.get_event_loop()
    async with _transcribe_sem:
        return await loop.run_in_executor(None, _transcribe_sync, audio_bytes, suffix)
