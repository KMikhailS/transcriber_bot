import logging
import os

from openai import OpenAI

from bot.audio_splitter import cleanup_chunks, split_audio
from bot.config import OPENAI_API_KEY, SUPPORTED_AUDIO_EXTENSIONS, WHISPER_MODEL

logger = logging.getLogger(__name__)

client = OpenAI(api_key=OPENAI_API_KEY)


class TranscriptionError(Exception):
    """Ошибка транскрибации."""


def validate_audio_file(file_path: str) -> None:
    """Проверить, что файл имеет поддерживаемый формат."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in SUPPORTED_AUDIO_EXTENSIONS:
        raise TranscriptionError(
            f"Неподдерживаемый формат файла: {ext}. "
            f"Поддерживаемые: {', '.join(sorted(SUPPORTED_AUDIO_EXTENSIONS))}"
        )


def _transcribe_single(file_path: str) -> str:
    """Транскрибировать один файл через Whisper API."""
    with open(file_path, "rb") as f:
        response = client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=f,
        )
    return response.text


def transcribe_audio(file_path: str) -> str:
    """Транскрибировать аудиофайл (с автоматической нарезкой, если нужно).

    1. Валидирует формат
    2. Нарезает на чанки, если файл > 24 МБ
    3. Транскрибирует каждый чанк
    4. Склеивает тексты
    5. Очищает временные файлы

    Returns:
        Текст транскрипции.

    Raises:
        TranscriptionError: при ошибке валидации или транскрибации.
    """
    validate_audio_file(file_path)

    chunk_paths = split_audio(file_path)
    logger.info("Чанков для транскрибации: %d", len(chunk_paths))

    texts: list[str] = []
    try:
        for i, chunk_path in enumerate(chunk_paths):
            logger.info("Транскрибирую чанк %d/%d: %s", i + 1, len(chunk_paths), chunk_path)
            text = _transcribe_single(chunk_path)
            texts.append(text)
            logger.info("Чанк %d: получено %d символов", i + 1, len(text))
    except Exception as exc:
        raise TranscriptionError(f"Ошибка при транскрибации: {exc}") from exc
    finally:
        cleanup_chunks(chunk_paths, original_path=file_path)

    result = " ".join(texts)
    logger.info("Транскрибация завершена: %d символов", len(result))
    return result
