import logging
import os
import tempfile
import time
from urllib.parse import unquote

import httpx

from bot.config import MAX_API_BASE_URL, MAX_BOT_TOKEN, POLLING_TIMEOUT

logger = logging.getLogger(__name__)


class MaxBotAPI:
    """HTTP-клиент для Max Bot API (https://dev.max.ru)."""

    def __init__(self) -> None:
        self._token = MAX_BOT_TOKEN
        self._base = MAX_API_BASE_URL
        self._client = httpx.Client(timeout=POLLING_TIMEOUT + 10)
        self._marker: int | None = None

    # ── helpers ──────────────────────────────────────────────

    def _params(self, **extra: object) -> dict:
        """Базовые query-параметры с токеном."""
        params: dict = {"access_token": self._token}
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    # ── long polling ─────────────────────────────────────────

    def get_updates(self) -> list[dict]:
        """Получить новые обновления (long polling)."""
        params = self._params(
            timeout=POLLING_TIMEOUT,
            marker=self._marker,
            types="message_created,bot_started,message_callback",
        )
        try:
            resp = self._client.get(self._url("/updates"), params=params)
            resp.raise_for_status()
            data = resp.json()
            updates = data.get("updates", [])
            if data.get("marker"):
                self._marker = data["marker"]
            return updates
        except httpx.HTTPError as exc:
            logger.error("Ошибка при получении обновлений: %s", exc)
            return []

    # ── сообщения ────────────────────────────────────────────

    def send_message(self, chat_id: int, text: str) -> dict | None:
        """Отправить текстовое сообщение в чат."""
        params = self._params(chat_id=chat_id)
        body = {"text": text, "format": "markdown"}
        try:
            resp = self._client.post(
                self._url("/messages"), params=params, json=body,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            logger.error("Ошибка отправки сообщения: %s", exc)
            return None

    def send_message_with_keyboard(
        self, chat_id: int, text: str, buttons: list[list[dict]],
    ) -> dict | None:
        """Отправить сообщение с inline-клавиатурой."""
        params = self._params(chat_id=chat_id)
        body = {
            "text": text,
            "attachments": [
                {
                    "type": "inline_keyboard",
                    "payload": {"buttons": buttons},
                },
            ],
        }
        try:
            resp = self._client.post(
                self._url("/messages"), params=params, json=body,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            logger.error("Ошибка отправки сообщения с клавиатурой: %s", exc)
            return None

    def answer_callback(self, callback_id: str, notification: str = "") -> dict | None:
        """Ответить на callback от inline-кнопки."""
        params = self._params(callback_id=callback_id)
        body: dict = {}
        if notification:
            body["notification"] = notification
        try:
            resp = self._client.post(
                self._url("/answers"), params=params, json=body,
            )
            if resp.status_code != 200:
                logger.warning("answer_callback %d: %s", resp.status_code, resp.text)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            logger.error("Ошибка ответа на callback: %s", exc)
            return None

    def edit_message(self, message_id: str, text: str) -> dict | None:
        """Редактировать текст существующего сообщения."""
        params = self._params(message_id=message_id)
        body = {"text": text}
        try:
            resp = self._client.put(
                self._url("/messages"), params=params, json=body,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            logger.error("Ошибка редактирования сообщения: %s", exc)
            return None

    # ── работа с файлами ─────────────────────────────────────

    def download_file(self, url: str, dest_dir: str | None = None) -> str:
        """Скачать файл по URL и вернуть путь к временному файлу."""
        if dest_dir is None:
            dest_dir = tempfile.mkdtemp(prefix="max_audio_")

        # Добавляем токен, если URL относительный
        download_url = url
        if not url.startswith("http"):
            download_url = self._url(url)

        resp = self._client.get(
            download_url, params=self._params(), follow_redirects=True,
        )
        resp.raise_for_status()

        # Определяем имя файла из заголовков или URL
        filename = _extract_filename(resp, url)
        filepath = os.path.join(dest_dir, filename)

        with open(filepath, "wb") as f:
            f.write(resp.content)

        logger.info("Файл скачан: %s (%d байт)", filepath, len(resp.content))
        return filepath

    def upload_file(self, file_path: str) -> dict | None:
        """Загрузить файл на сервер Max. Возвращает ответ API с URL/token."""
        # Шаг 1: получить URL для загрузки
        params = self._params(type="file")
        try:
            resp = self._client.post(self._url("/uploads"), params=params)
            resp.raise_for_status()
            upload_info = resp.json()
            logger.info("Upload info: %s", upload_info)
        except httpx.HTTPError as exc:
            logger.error("Ошибка получения URL загрузки: %s", exc)
            return None

        upload_url = upload_info.get("url")
        if not upload_url:
            logger.error("Нет URL в ответе /uploads: %s", upload_info)
            return None

        # Шаг 2: загрузить файл по полученному URL
        try:
            with open(file_path, "rb") as f:
                resp = self._client.post(
                    upload_url,
                    files={"data": (os.path.basename(file_path), f)},
                )
            resp.raise_for_status()
            file_info = resp.json()
            logger.info("File upload result: %s", file_info)
            return file_info
        except httpx.HTTPError as exc:
            logger.error("Ошибка загрузки файла: %s", exc)
            return None

    def send_file(
        self, chat_id: int, file_path: str,
        text: str = " ",
        keyboard_buttons: list[list[dict]] | None = None,
    ) -> dict | None:
        """Загрузить файл и отправить его в чат.

        keyboard_buttons — если передан, добавляет inline_keyboard к сообщению.
        """
        upload_result = self.upload_file(file_path)
        if not upload_result:
            return None

        # Формируем вложение в формате Max API
        file_attachment = {
            "type": "file",
            "payload": {
                "token": upload_result.get("token"),
            },
        }
        if upload_result.get("fileId"):
            file_attachment["payload"]["fileId"] = upload_result["fileId"]

        attachments: list[dict] = [file_attachment]

        if keyboard_buttons:
            attachments.append({
                "type": "inline_keyboard",
                "payload": {"buttons": keyboard_buttons},
            })

        logger.info("Отправляю вложение: %s", attachments)

        params = self._params(chat_id=chat_id)
        body = {
            "text": text,
            "attachments": attachments,
        }

        # Повторные попытки — сервер может не успеть обработать файл
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                resp = self._client.post(
                    self._url("/messages"), params=params, json=body,
                )
                if resp.status_code == 200:
                    return resp.json()

                error_body = resp.text
                logger.warning(
                    "Попытка %d/%d — ответ %d: %s",
                    attempt, max_retries, resp.status_code, error_body,
                )

                # Если файл ещё не обработан — ждём и пробуем снова
                if "attachment.not.ready" in error_body and attempt < max_retries:
                    delay = attempt * 2  # 2, 4, 6, 8 секунд
                    logger.info("Файл ещё обрабатывается, жду %d сек...", delay)
                    time.sleep(delay)
                    continue

                # Другая ошибка — не повторяем
                resp.raise_for_status()

            except httpx.HTTPError as exc:
                logger.error("Ошибка отправки файла: %s", exc)
                return None

        logger.error("Не удалось отправить файл после %d попыток", max_retries)
        return None

    def close(self) -> None:
        self._client.close()


def _extract_filename(resp: httpx.Response, url: str) -> str:
    """Извлечь имя файла из Content-Disposition или URL."""
    cd = resp.headers.get("content-disposition", "")
    if "filename=" in cd:
        # Парсим filename из Content-Disposition, игнорируя filename* (RFC 5987)
        for part in cd.split(";"):
            part = part.strip()
            if part.startswith("filename=") and not part.startswith("filename*="):
                name = part.split("=", 1)[1].strip('" ')
                if name:
                    return unquote(name)
    # Берём последнюю часть URL без query-параметров
    basename = url.split("?")[0].split("/")[-1]
    return unquote(basename) if basename else "audio_file"
