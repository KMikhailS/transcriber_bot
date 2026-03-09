import logging
import os
import tempfile

from pydub import AudioSegment

from bot.config import MAX_FILE_SIZE_MB

logger = logging.getLogger(__name__)

MAX_CHUNK_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024  # 24 МБ в байтах


def split_audio(file_path: str) -> list[str]:
    """Разбить аудиофайл на чанки, если он превышает лимит.

    Если файл ≤ MAX_FILE_SIZE_MB — возвращает [file_path].
    Если файл больше — нарезает на части и возвращает список путей к чанкам.
    """
    file_size = os.path.getsize(file_path)

    if file_size <= MAX_CHUNK_BYTES:
        logger.info("Файл %s (%d байт) — нарезка не требуется", file_path, file_size)
        return [file_path]

    logger.info(
        "Файл %s (%d байт) превышает лимит %d МБ — нарезаю на чанки",
        file_path, file_size, MAX_FILE_SIZE_MB,
    )

    audio = AudioSegment.from_file(file_path)
    total_duration_ms = len(audio)

    # Вычисляем длительность одного чанка на основе битрейта
    # chunk_duration = (max_size / file_size) * total_duration
    num_chunks = (file_size // MAX_CHUNK_BYTES) + 1
    chunk_duration_ms = total_duration_ms // num_chunks

    chunk_dir = tempfile.mkdtemp(prefix="audio_chunks_")
    chunk_paths: list[str] = []

    for i in range(num_chunks):
        start_ms = i * chunk_duration_ms
        end_ms = min((i + 1) * chunk_duration_ms, total_duration_ms)
        chunk = audio[start_ms:end_ms]

        chunk_path = os.path.join(chunk_dir, f"chunk_{i:03d}.mp3")
        chunk.export(chunk_path, format="mp3", bitrate="128k")
        chunk_paths.append(chunk_path)

        logger.info(
            "Чанк %d: %d-%d мс → %s (%d байт)",
            i, start_ms, end_ms, chunk_path, os.path.getsize(chunk_path),
        )

    return chunk_paths


def cleanup_chunks(chunk_paths: list[str], original_path: str) -> None:
    """Удалить временные файлы чанков (не трогает оригинал)."""
    for path in chunk_paths:
        if path != original_path:
            try:
                os.remove(path)
            except OSError:
                pass

    # Удаляем временную директорию, если она пуста
    if chunk_paths and chunk_paths[0] != original_path:
        chunk_dir = os.path.dirname(chunk_paths[0])
        try:
            os.rmdir(chunk_dir)
        except OSError:
            pass
