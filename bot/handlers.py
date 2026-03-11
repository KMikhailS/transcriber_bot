import json
import logging
import os
import tempfile

from bot.max_api import MaxBotAPI
from bot.transcriber import TranscriptionError, transcribe_audio

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "👋 Привет! Я Стенограф — бот для расшифровки аудио в текст.\n\n"
    "Отправь мне аудиофайл (mp3, wav, ogg, m4a и др.), "
    "и я верну текстовый файл с расшифровкой.\n\n"
    "Поддерживаемые форматы: mp3, mp4, m4a, wav, webm, ogg, mpeg, mpga."
)

PROCESSING_TEXT = "⏳ Транскрибирую аудио… 0%"

INVALID_FILE_TEXT = (
    "❌ Пожалуйста, отправьте аудиофайл.\n"
    "Поддерживаемые форматы: mp3, mp4, m4a, wav, webm, ogg, mpeg, mpga."
)


def handle_update(api: MaxBotAPI, update: dict) -> None:
    """Обработать одно обновление от Max API."""
    logger.info("Получено обновление: %s", json.dumps(update, ensure_ascii=False, default=str))

    # Обработка события старта диалога с ботом
    if update.get("update_type") == "bot_started":
        chat_id = update.get("chat_id")
        if chat_id:
            api.send_message(chat_id, WELCOME_TEXT)
        return

    message = update.get("message")
    if not message:
        logger.warning("Обновление без поля 'message', пропускаю")
        return

    # Извлекаем chat_id из разных возможных мест
    chat_id = (
        message.get("recipient", {}).get("chat_id")
        or message.get("chat_id")
        or message.get("chatId")
    )
    if not chat_id:
        logger.warning("Не удалось определить chat_id из сообщения: %s", json.dumps(message, ensure_ascii=False, default=str))
        return

    body = message.get("body", {})

    # Проверка на команду /start
    text = body.get("text", "")
    if text.strip() == "/start":
        api.send_message(chat_id, WELCOME_TEXT)
        return

    # Проверка на наличие вложений (аудиофайл)
    attachments = body.get("attachments", [])
    logger.info("Вложения: %s", json.dumps(attachments, ensure_ascii=False, default=str))

    audio_attachment = _find_audio_attachment(attachments)

    if not audio_attachment:
        api.send_message(chat_id, INVALID_FILE_TEXT)
        return

    _handle_audio(api, chat_id, audio_attachment)


def _find_audio_attachment(attachments: list[dict]) -> dict | None:
    """Найти аудиовложение среди всех вложений."""
    for att in attachments:
        att_type = att.get("type", "")
        logger.info("Тип вложения: '%s'", att_type)
        # Max может отправлять аудио как "file", "audio" или "voice"
        if att_type in ("file", "audio", "voice"):
            return att
    return None


def _handle_audio(api: MaxBotAPI, chat_id: int, attachment: dict) -> None:
    """Обработать аудиовложение: скачать, транскрибировать, отправить результат."""
    # Получаем URL файла из вложения
    payload = attachment.get("payload", {})
    file_url = payload.get("url")

    if not file_url:
        api.send_message(chat_id, "❌ Не удалось получить ссылку на файл.")
        return

    status_resp = api.send_message(chat_id, PROCESSING_TEXT)
    status_mid = _extract_message_id(status_resp)

    tmp_dir = tempfile.mkdtemp(prefix="transcriber_")
    audio_path = None
    txt_path = None

    def _report_progress(current: int, total: int) -> None:
        """Обновить сообщение-статус с текущим прогрессом."""
        if status_mid is None:
            return
        pct = current * 100 // total
        api.edit_message(status_mid, f"⏳ Транскрибирую аудио… {pct}%")

    try:
        # 1. Скачиваем аудиофайл
        audio_path = api.download_file(file_url, dest_dir=tmp_dir)
        logger.info("Скачан файл: %s", audio_path)

        # 2. Транскрибируем
        text = transcribe_audio(audio_path, on_progress=_report_progress)

        if not text.strip():
            api.send_message(chat_id, "⚠️ Не удалось распознать речь в аудио.")
            return

        # 3. Сохраняем результат в .txt (имя файла совпадает с аудио)
        audio_stem = os.path.splitext(os.path.basename(audio_path))[0]
        txt_path = os.path.join(tmp_dir, audio_stem + ".txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)

        # 4. Обновляем статус — отправка файла
        if status_mid:
            api.edit_message(status_mid, "⏳ Отправляю результат…")

        # 5. Отправляем файл пользователю
        result = api.send_file(chat_id, txt_path)
        if result:
            logger.info("Транскрипция отправлена в чат %s", chat_id)
            # Если текст короткий — дублируем его сообщением в чат
            if len(text) < 1500:
                api.send_message(chat_id, text)
            if status_mid:
                api.edit_message(status_mid, "✅ Транскрибация завершена!")
        else:
            api.send_message(chat_id, "❌ Не удалось отправить файл с транскрипцией.")

    except TranscriptionError as exc:
        logger.error("Ошибка транскрибации: %s", exc)
        api.send_message(chat_id, f"❌ {exc}")

    except Exception as exc:
        logger.exception("Непредвиденная ошибка: %s", exc)
        api.send_message(chat_id, "❌ Произошла ошибка при обработке файла.")

    finally:
        # 6. Очистка временных файлов
        _cleanup_tmp(tmp_dir)


def _extract_message_id(resp: dict | None) -> str | None:
    """Извлечь mid (идентификатор сообщения) из ответа send_message."""
    if not resp:
        return None
    body = resp.get("message", {}).get("body", {})
    return body.get("mid")


def _cleanup_tmp(tmp_dir: str) -> None:
    """Удалить временную директорию со всеми файлами."""
    try:
        for f in os.listdir(tmp_dir):
            os.remove(os.path.join(tmp_dir, f))
        os.rmdir(tmp_dir)
    except OSError as exc:
        logger.warning("Не удалось очистить %s: %s", tmp_dir, exc)
