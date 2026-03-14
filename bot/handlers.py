import json
import logging
import os
import tempfile
import threading
import time
import uuid

from bot.database import get_or_create_user, get_user_balance, mark_payment_paid, save_payment
from bot.formatter import format_text
from bot.link_downloader import download_audio_from_url, extract_media_url
from bot.max_api import MaxBotAPI
from bot.payment import create_payment, get_payment_status
from bot.summarizer import summarize_text
from bot.transcriber import TranscriptionError, transcribe_audio

logger = logging.getLogger(__name__)

# Хранилище контекста транскрибации для кнопки "Сделать саммари"
# Ключ: callback payload (уникальный ID), значение: (text, audio_stem)
_summary_context: dict[str, tuple[str, str]] = {}

WELCOME_TEXT = (
    "👋 Привет! Я Стенограф — бот для расшифровки аудио в текст.\n\n"
    "Отправь мне аудиофайл (mp3, wav, ogg, m4a и др.) "
    "или ссылку на YouTube / Instagram видео, "
    "и я верну текстовый файл с расшифровкой.\n\n"
    "Поддерживаемые форматы: mp3, mp4, m4a, wav, webm, ogg, mpeg, mpga.\n"
    "Ссылки: YouTube, Instagram (Reels, посты с видео)."
)

DOWNLOADING_TEXT = "⏳ Скачиваю аудио…"
PREPARING_TEXT = "⏳ Подготавливаю аудио…"

INVALID_FILE_TEXT = (
    "❌ Пожалуйста, отправьте аудиофайл или ссылку на YouTube / Instagram видео.\n"
    "Поддерживаемые форматы: mp3, mp4, m4a, wav, webm, ogg, mpeg, mpga.\n"
    "Ссылки: YouTube, Instagram (Reels, посты с видео)."
)


def handle_update(api: MaxBotAPI, update: dict) -> None:
    """Обработать одно обновление от Max API."""
    logger.info("Получено обновление: %s", json.dumps(update, ensure_ascii=False, default=str))

    # Обработка события старта диалога с ботом
    if update.get("update_type") == "bot_started":
        chat_id = update.get("chat_id")
        user = update.get("user", {})
        if chat_id:
            _register_user(user)
            _send_welcome(api, chat_id)
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
        sender = message.get("sender", {})
        _register_user(sender)
        _send_welcome(api, chat_id)
        return

    # Проверка на ссылку YouTube/Instagram в тексте
    media_url = extract_media_url(text)
    if media_url:
        _handle_url(api, chat_id, media_url)
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

    try:
        audio_path = api.download_file(file_url, dest_dir=tmp_dir)
        logger.info("Скачан файл: %s", audio_path)

        _process_audio(api, chat_id, audio_path, tmp_dir, status_mid)

    except TranscriptionError as exc:
        logger.error("Ошибка транскрибации: %s", exc)
        api.send_message(chat_id, f"❌ {exc}")

    except Exception as exc:
        logger.exception("Непредвиденная ошибка: %s", exc)
        api.send_message(chat_id, "❌ Произошла ошибка при обработке файла.")

    finally:
        _cleanup_tmp(tmp_dir)


def _handle_url(api: MaxBotAPI, chat_id: int, url: str) -> None:
    """Обработать ссылку на YouTube/Instagram: скачать аудио, транскрибировать."""
    status_resp = api.send_message(chat_id, "⏳ Скачиваю аудио по ссылке…")
    status_mid = _extract_message_id(status_resp)

    tmp_dir = tempfile.mkdtemp(prefix="transcriber_url_")

    try:
        audio_path = download_audio_from_url(url, tmp_dir)
        logger.info("Аудио скачано из URL: %s → %s", url, audio_path)

        _process_audio(api, chat_id, audio_path, tmp_dir, status_mid)

    except RuntimeError as exc:
        logger.error("Ошибка скачивания по ссылке: %s", exc)
        api.send_message(chat_id, f"❌ {exc}")

    except TranscriptionError as exc:
        logger.error("Ошибка транскрибации: %s", exc)
        api.send_message(chat_id, f"❌ {exc}")

    except Exception as exc:
        logger.exception("Непредвиденная ошибка: %s", exc)
        api.send_message(chat_id, "❌ Произошла ошибка при обработке ссылки.")

    finally:
        _cleanup_tmp(tmp_dir)


def _process_audio(
    api: MaxBotAPI,
    chat_id: int,
    audio_path: str,
    tmp_dir: str,
    status_mid: str | None,
) -> None:
    """Общая логика: транскрибация → форматирование → отправка результата.

    Вызывается из _handle_audio() и _handle_url().
    """

    def _report_progress(current: int, total: int) -> None:
        if status_mid is None:
            return
        pct = current * 100 // total
        api.edit_message(status_mid, f"⏳ Транскрибирую аудио… {pct}%")

    # 1. Подготовка аудио (нарезка на чанки)
    if status_mid:
        api.edit_message(status_mid, PREPARING_TEXT)

    # 2. Транскрибируем
    text = transcribe_audio(audio_path, on_progress=_report_progress)

    if not text.strip():
        api.send_message(chat_id, "⚠️ Не удалось распознать речь в аудио.")
        return

    # 3. Форматируем текст через Claude
    if status_mid:
        api.edit_message(status_mid, "⏳ Форматирую текст…")
    text = format_text(text)

    # 4. Сохраняем результат в .txt (имя файла совпадает с аудио)
    audio_stem = os.path.splitext(os.path.basename(audio_path))[0]
    txt_path = os.path.join(tmp_dir, audio_stem + ".txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)

    # 5. Обновляем статус — отправка файла
    if status_mid:
        api.edit_message(status_mid, "⏳ Отправляю результат…")

    # 6. Сохраняем контекст для кнопки саммари
    callback_id = uuid.uuid4().hex[:16]
    _summary_context[callback_id] = (text, audio_stem)

    # 7. Отправляем файл с кнопкой "Получить краткий конспект"
    summary_button = [[{"type": "callback", "text": "📝 Получить краткий конспект", "payload": callback_id}]]
    result = api.send_file(chat_id, txt_path, keyboard_buttons=summary_button)
    if result:
        logger.info("Транскрипция отправлена в чат %s", chat_id)
        if len(text) < 4096:
            api.send_message(chat_id, text)
        if status_mid:
            api.edit_message(status_mid, "✅ Транскрибация завершена!")
    else:
        api.send_message(chat_id, "❌ Не удалось отправить файл с транскрипцией.")


def _send_welcome(api: MaxBotAPI, chat_id: int) -> None:
    """Отправить приветственное сообщение с кнопкой 'Подписка'."""
    buttons = [[{"type": "callback", "text": "💳 Подписка", "payload": "sub_info"}]]
    api.send_message_with_keyboard(chat_id, WELCOME_TEXT, buttons)


def _handle_callback(api: MaxBotAPI, update: dict) -> None:
    """Обработать нажатие inline-кнопки."""
    callback = update.get("callback", {})
    callback_id = callback.get("callback_id")
    payload = callback.get("payload", "")

    # Определяем chat_id и user_id из callback
    message = update.get("message", {})
    chat_id = (
        message.get("recipient", {}).get("chat_id")
        or message.get("chat_id")
    )
    # В message_callback пользователь может быть в callback.user или message.sender
    user = callback.get("user") or update.get("user") or message.get("sender") or {}
    user_id = user.get("user_id")
    logger.info("callback user_id=%s, update keys=%s, callback keys=%s", user_id, list(update.keys()), list(callback.keys()))

    if not chat_id:
        logger.warning("Не удалось определить chat_id из callback: %s", json.dumps(update, ensure_ascii=False, default=str))
        return

    # Отвечаем на callback (убираем "часики" на кнопке)
    if callback_id:
        api.answer_callback(callback_id)

    # --- Подписка: маршрутизация по payload ---
    if payload == "sub_info":
        _handle_sub_info(api, chat_id, user_id)
        return

    if payload == "sub_pay":
        _handle_sub_pay(api, chat_id)
        return

    if payload == "sub_topup":
        _handle_sub_topup(api, chat_id, user_id)
        return

    if payload == "sub_back":
        _send_welcome(api, chat_id)
        return

    # --- Саммари ---
    context = _summary_context.pop(payload, None)
    if not context:
        api.send_message(chat_id, "⚠️ Данные для саммари не найдены. Попробуйте отправить аудио ещё раз.")
        return

    text, audio_stem = context
    _handle_summary(api, chat_id, text, audio_stem)


def _handle_sub_info(api: MaxBotAPI, chat_id: int, user_id: int | None) -> None:
    """Показать баланс пользователя и кнопки оплаты."""
    balance = get_user_balance(user_id) if user_id else 0
    text = f"💰 Ваш текущий баланс: {balance} руб."
    buttons = [
        [{"type": "callback", "text": "💳 Оплатить подписку", "payload": "sub_pay"}],
        [{"type": "callback", "text": "⬅️ Назад", "payload": "sub_back"}],
    ]
    api.send_message_with_keyboard(chat_id, text, buttons)


def _handle_sub_pay(api: MaxBotAPI, chat_id: int) -> None:
    """Показать кнопку пополнения на 1000 рублей."""
    buttons = [
        [{"type": "callback", "text": "💵 Пополнить на 1000 рублей", "payload": "sub_topup"}],
        [{"type": "callback", "text": "⬅️ Назад", "payload": "sub_info"}],
    ]
    api.send_message_with_keyboard(chat_id, "Выберите сумму пополнения:", buttons)


def _handle_sub_topup(api: MaxBotAPI, chat_id: int, user_id: int | None) -> None:
    """Создать платёж в ЮKassa и показать кнопку-ссылку для оплаты."""
    api.send_message(chat_id, "⏳ Создаю платёж…")

    result = create_payment(1000, "Пополнение баланса на 1000 руб.")
    if not result:
        api.send_message(chat_id, "❌ Не удалось создать платёж. Попробуйте позже.")
        return

    payment_id, payment_url = result

    if user_id:
        try:
            save_payment(payment_id, user_id, 1000)
        except Exception as exc:
            logger.error("Ошибка сохранения платежа %s: %s", payment_id, exc)

    buttons = [
        [{"type": "link", "text": "💳 Оплатить", "url": payment_url}],
    ]
    api.send_message_with_keyboard(chat_id, "Нажмите кнопку ниже для оплаты:", buttons)

    if user_id:
        threading.Thread(
            target=_poll_payment,
            args=(api, chat_id, user_id, payment_id, 1000),
            daemon=True,
        ).start()


def _poll_payment(
    api: MaxBotAPI, chat_id: int, user_id: int, payment_id: str, amount: int,
) -> None:
    """Поллинг статуса платежа каждые 5 сек, до 10 минут."""
    deadline = time.time() + 600
    interval = 5
    while time.time() < deadline:
        time.sleep(interval)
        try:
            status = get_payment_status(payment_id)
        except Exception as exc:
            logger.error("Ошибка проверки статуса платежа %s: %s", payment_id, exc)
            continue

        if status == "succeeded":
            try:
                mark_payment_paid(payment_id, user_id, amount)
                new_balance = get_user_balance(user_id)
                api.send_message(
                    chat_id,
                    f"✅ Оплата прошла успешно! Ваш новый баланс: {new_balance} руб.",
                )
            except Exception as exc:
                logger.error("Ошибка зачисления платежа %s: %s", payment_id, exc)
            return

        if status == "canceled":
            api.send_message(chat_id, "❌ Платёж отменён.")
            return


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
            if len(summary) < 4096:
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


def _register_user(user: dict) -> None:
    """Зарегистрировать пользователя в БД если ещё не существует."""
    user_id = user.get("user_id")
    if not user_id:
        return
    username = user.get("username")
    first_name = user.get("name")
    try:
        get_or_create_user(user_id, username, first_name)
    except Exception as exc:
        logger.error("Ошибка регистрации пользователя %s: %s", user_id, exc)


def _cleanup_tmp(tmp_dir: str) -> None:
    """Удалить временную директорию со всеми файлами."""
    try:
        for f in os.listdir(tmp_dir):
            os.remove(os.path.join(tmp_dir, f))
        os.rmdir(tmp_dir)
    except OSError as exc:
        logger.warning("Не удалось очистить %s: %s", tmp_dir, exc)
