import logging
import os
from dataclasses import dataclass

from openai import OpenAI

from bot.audio_splitter import cleanup_chunks, split_audio
from bot.config import OPENAI_API_KEY, SUPPORTED_AUDIO_EXTENSIONS, WHISPER_MODEL
from bot.diarizer import DiarizationSegment, diarize, find_speaker

logger = logging.getLogger(__name__)

client = OpenAI(api_key=OPENAI_API_KEY)


class TranscriptionError(Exception):
    """Ошибка транскрибации."""


@dataclass
class TranscriptionSegment:
    """Сегмент транскрибации с таймкодами."""
    start: float
    end: float
    text: str


def validate_audio_file(file_path: str) -> None:
    """Проверить, что файл имеет поддерживаемый формат."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in SUPPORTED_AUDIO_EXTENSIONS:
        raise TranscriptionError(
            f"Неподдерживаемый формат файла: {ext}. "
            f"Поддерживаемые: {', '.join(sorted(SUPPORTED_AUDIO_EXTENSIONS))}"
        )


def _transcribe_single(file_path: str) -> list[TranscriptionSegment]:
    """Транскрибировать один файл через Whisper API с таймкодами."""
    with open(file_path, "rb") as f:
        response = client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )

    segments: list[TranscriptionSegment] = []
    for seg in response.segments or []:
        segments.append(TranscriptionSegment(
            start=seg.start,
            end=seg.end,
            text=seg.text.strip(),
        ))
    return segments


def _merge_with_diarization(
    transcription_segments: list[TranscriptionSegment],
    diarization_segments: list[DiarizationSegment],
) -> str:
    """Совместить транскрибацию с диаризацией и отформатировать результат.

    Для каждого сегмента текста находим спикера по максимальному пересечению
    таймкодов. Последовательные реплики одного спикера объединяются.
    """
    if not transcription_segments:
        return ""

    lines: list[str] = []
    current_speaker: str | None = None
    current_texts: list[str] = []

    for seg in transcription_segments:
        speaker = find_speaker(diarization_segments, seg.start, seg.end)

        if speaker == current_speaker:
            # Тот же спикер — добавляем текст к текущей реплике
            current_texts.append(seg.text)
        else:
            # Новый спикер — сохраняем предыдущую реплику
            if current_speaker is not None and current_texts:
                lines.append(f"[{current_speaker}] {' '.join(current_texts)}")
            current_speaker = speaker
            current_texts = [seg.text]

    # Последняя реплика
    if current_speaker is not None and current_texts:
        lines.append(f"[{current_speaker}] {' '.join(current_texts)}")

    return "\n\n".join(lines)


def transcribe_audio(file_path: str) -> str:
    """Транскрибировать аудиофайл с определением спикеров.

    1. Валидирует формат
    2. Запускает диаризацию (pyannote — определение спикеров)
    3. Нарезает на чанки, если файл > 24 МБ
    4. Транскрибирует каждый чанк через Whisper API (с таймкодами)
    5. Совмещает текст с диаризацией
    6. Очищает временные файлы

    Returns:
        Текст транскрипции с метками спикеров.

    Raises:
        TranscriptionError: при ошибке валидации или транскрибации.
    """
    validate_audio_file(file_path)

    # 1. Диаризация — определяем КТО и КОГДА говорит
    logger.info("Запускаю диаризацию...")
    try:
        diarization_segments = diarize(file_path)
    except Exception as exc:
        raise TranscriptionError(f"Ошибка диаризации: {exc}") from exc

    # 2. Транскрибация — определяем ЧТО говорят (с таймкодами)
    chunk_paths = split_audio(file_path)
    logger.info("Чанков для транскрибации: %d", len(chunk_paths))

    all_segments: list[TranscriptionSegment] = []
    time_offset = 0.0

    try:
        for i, chunk_path in enumerate(chunk_paths):
            logger.info("Транскрибирую чанк %d/%d: %s", i + 1, len(chunk_paths), chunk_path)
            segments = _transcribe_single(chunk_path)

            # Для чанков после первого — сдвигаем таймкоды
            if time_offset > 0:
                for seg in segments:
                    seg.start += time_offset
                    seg.end += time_offset

            all_segments.extend(segments)

            # Обновляем сдвиг: конец последнего сегмента этого чанка
            if segments:
                time_offset = segments[-1].end

            logger.info("Чанк %d: получено %d сегментов", i + 1, len(segments))
    except Exception as exc:
        raise TranscriptionError(f"Ошибка при транскрибации: {exc}") from exc
    finally:
        cleanup_chunks(chunk_paths, original_path=file_path)

    # 3. Совмещаем транскрибацию с диаризацией
    result = _merge_with_diarization(all_segments, diarization_segments)
    logger.info("Транскрибация завершена: %d символов, %d сегментов",
                len(result), len(all_segments))
    return result
