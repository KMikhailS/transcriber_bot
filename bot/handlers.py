import json
import logging
import os
import tempfile
import uuid

from bot.formatter import format_text
from bot.max_api import MaxBotAPI
from bot.summarizer import summarize_text
from bot.transcriber import TranscriptionError, transcribe_audio

logger = logging.getLogger(__name__)

# Хранилище контекста транскрибации для кнопки "Сделать саммари"
# Ключ: callback payload (уникальный ID), значение: (text, audio_stem)
_summary_context: dict[str, tuple[str, str]] = {}

WELCOME_TEXT = (
    "👋 Привет! Я Стенограф — бот для расшифровки аудио в текст.\n\n"
    "Отправь мне аудиофайл (mp3, wav, ogg, m4a и др.), "
    "и я верну текстовый файл с расшифровкой.\n\n"
    "Поддерживаемые форматы: mp3, mp4, m4a, wav, webm, ogg, mpeg, mpga."
)

DOWNLOADING_TEXT = "⏳ Скачиваю аудио…"
PREPARING_TEXT = "⏳ Подготавливаю аудио…"

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

    # Обработка нажатия inline-кнопки
    if update.get("update_type") == "message_callback":
        _handle_callback(api, update)
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

    status_resp = api.send_message(chat_id, DOWNLOADING_TEXT)
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

        # 2. Обновляем статус — подготовка аудио (нарезка на чанки)
        if status_mid:
            api.edit_message(status_mid, PREPARING_TEXT)

        # 3. Транскрибируем
        text = transcribe_audio(audio_path, on_progress=_report_progress)

        if not text.strip():
            api.send_message(chat_id, "⚠️ Не удалось распознать речь в аудио.")
            return

        # 4. Форматируем текст через Claude
        if status_mid:
            api.edit_message(status_mid, "⏳ Форматирую текст…")
        text = format_text(text)

        # 5. Сохраняем результат в .txt (имя файла совпадает с аудио)
        audio_stem = os.path.splitext(os.path.basename(audio_path))[0]
        txt_path = os.path.join(tmp_dir, audio_stem + ".txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)

        # 6. Обновляем статус — отправка файла
        if status_mid:
            api.edit_message(status_mid, "⏳ Отправляю результат…")

        # 7. Отправляем файл пользователю
        result = api.send_file(chat_id, txt_path)
        if result:
            logger.info("Транскрипция отправлена в чат %s", chat_id)
            # Если текст короткий — дублируем его сообщением в чат
            if len(text) < 2500:
                api.send_message(chat_id, text)

            # 8. Сохраняем контекст и отправляем кнопку "Сделать саммари"
            callback_id = uuid.uuid4().hex[:16]
            _summary_context[callback_id] = (text, audio_stem)

            if status_mid:
                api.edit_message(status_mid, "✅ Транскрибация завершена!")

            api.send_message_with_keyboard(
                chat_id,
                "📝 Хотите получить краткое содержание?",
                buttons=[[{"type": "callback", "text": "📝 Сделать саммари", "payload": callback_id}]],
            )
        else:
            api.send_message(chat_id, "❌ Не удалось отправить файл с транскрипцией.")

    except TranscriptionError as exc:
        logger.error("Ошибка транскрибации: %s", exc)
        api.send_message(chat_id, f"❌ {exc}")

    except Exception as exc:
        logger.exception("Непредвиденная ошибка: %s", exc)
        api.send_message(chat_id, "❌ Произошла ошибка при обработке файла.")

    finally:
        # 8. Очистка временных файлов
        _cleanup_tmp(tmp_dir)


def _handle_callback(api: MaxBotAPI, update: dict) -> None:
    """Обработать нажатие inline-кнопки."""
    callback = update.get("callback", {})
    callback_id = callback.get("callback_id")
    payload = callback.get("payload", "")

    # Определяем chat_id из callback
    message = update.get("message", {})
    chat_id = (
        message.get("recipient", {}).get("chat_id")
        or message.get("chat_id")
    )

    if not chat_id:
        logger.warning("Не удалось определить chat_id из callback: %s", json.dumps(update, ensure_ascii=False, default=str))
        return

    # Отвечаем на callback (убираем "часики" на кнопке)
    if callback_id:
        api.answer_callback(callback_id)

    # Проверяем, есть ли контекст для этого payload
    context = _summary_context.pop(payload, None)
    if not context:
        api.send_message(chat_id, "⚠️ Данные для саммари не найдены. Попробуйте отправить аудио ещё раз.")
        return

    text, audio_stem = context
    _handle_summary(api, chat_id, text, audio_stem)


def _handle_summary(api: MaxBotAPI, chat_id: int, text: str, audio_stem: str) -> None:
    """Создать саммари и отправить результат."""
    status_resp = api.send_message(chat_id, "⏳ Создаю саммари…")
    status_mid = _extract_message_id(status_resp)

    tmp_dir = tempfile.mkdtemp(prefix="summary_")

    try:
        summary = summarize_text(text)
        if not summary:
            api.send_message(chat_id, "❌ Не удалось создать саммари.")
            return

        # Сохраняем саммари в файл
        summary_filename = f"summary_{audio_stem}.txt"
        summary_path = os.path.join(tmp_dir, summary_filename)
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(summary)

        # Обновляем статус
        if status_mid:
            api.edit_message(status_mid, "⏳ Отправляю саммари…")

        # Отправляем файл
        result = api.send_file(chat_id, summary_path)
        if result:
            logger.info("Саммари отправлено в чат %s", chat_id)
            # Если саммари короткое — дублируем текстом в чат
            if len(summary) < 2500:
                api.send_message(chat_id, summary)
            if status_mid:
                api.edit_message(status_mid, "✅ Саммари готово!")
        else:
            api.send_message(chat_id, "❌ Не удалось отправить файл с саммари.")

    except Exception as exc:
        logger.exception("Ошибка при создании саммари: %s", exc)
        api.send_message(chat_id, "❌ Произошла ошибка при создании саммари.")

    finally:
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
