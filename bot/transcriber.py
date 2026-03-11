import logging
import os
import re

from openai import OpenAI

from bot.audio_splitter import cleanup_chunks, split_audio
from bot.config import (
    OPENAI_API_KEY,
    SUPPORTED_AUDIO_EXTENSIONS,
    WHISPER_LANGUAGE,
    WHISPER_MODEL,
)

logger = logging.getLogger(__name__)

client = OpenAI(api_key=OPENAI_API_KEY)

# Порог: если одно и то же слово/фраза повторяется подряд >= этого числа раз,
# считаем это галлюцинацией и удаляем
_HALLUCINATION_REPEAT_THRESHOLD = 4

# Паттерн для обнаружения повторяющихся коротких токенов (1-15 символов),
# повторяющихся подряд много раз — типичный признак hallucination loop
_HALLUCINATION_PATTERN = re.compile(
    r"(?:\s|^)"                   # начало или пробел
    r"([\w!?]{1,15})"            # захватываем короткое слово/токен
    r"((?:[!.,]?\s+\1){" +       # то же слово повторяется через пробел
    str(_HALLUCINATION_REPEAT_THRESHOLD - 1) +
    r",})",                       # минимум N-1 повтор (итого N раз)
    re.UNICODE,
)


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


def _clean_hallucinations(text: str) -> str:
    """Удалить из текста повторяющиеся залипания (hallucination loops).

    Whisper иногда при шумах генерирует сотни повторов одного токена:
    "Мг! Мг! Мг! Мг! ..." или "Ооо! Ооо! Ооо! ...".
    Эта функция обнаруживает такие паттерны и удаляет их.
    """
    original_len = len(text)

    # Удаляем повторяющиеся фразы/слова
    cleaned = _HALLUCINATION_PATTERN.sub("", text)

    # Нормализуем множественные пробелы
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    removed_chars = original_len - len(cleaned)
    if removed_chars > 0:
        logger.info(
            "Обнаружены и удалены галлюцинации Whisper: удалено %d символов (%.1f%%)",
            removed_chars,
            removed_chars / original_len * 100,
        )

    return cleaned


def _transcribe_single(file_path: str) -> str:
    """Транскрибировать один файл через Whisper API."""
    with open(file_path, "rb") as f:
        response = client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=f,
            language=WHISPER_LANGUAGE,
            temperature=0.0,
        )
    return response.text


def transcribe_audio(file_path: str) -> str:
    """Транскрибировать аудиофайл (с автоматической нарезкой, если нужно).

    1. Валидирует формат
    2. Нарезает на чанки, если файл > 24 МБ
    3. Транскрибирует каждый чанк
    4. Склеивает тексты
    5. Удаляет галлюцинации (повторяющиеся токены)
    6. Очищает временные файлы

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
    logger.info("Транскрибация завершена: %d символов (до очистки)", len(result))

    # Постобработка: удаление hallucination loops
    result = _clean_hallucinations(result)
    logger.info("После очистки галлюцинаций: %d символов", len(result))

    return result
