import os
import sys

from dotenv import load_dotenv

load_dotenv()

MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Max Bot API
MAX_API_BASE_URL = "https://botapi.max.ru"
POLLING_TIMEOUT = 30  # секунд

# Whisper
WHISPER_MODEL = "whisper-1"
MAX_FILE_SIZE_MB = 24  # лимит Whisper 25 МБ, берём с запасом
SUPPORTED_AUDIO_EXTENSIONS = {
    ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg",
}


def validate_config() -> None:
    """Проверяет, что все обязательные переменные окружения заданы."""
    if not MAX_BOT_TOKEN:
        sys.exit("Ошибка: переменная MAX_BOT_TOKEN не задана")
    if not OPENAI_API_KEY:
        sys.exit("Ошибка: переменная OPENAI_API_KEY не задана")
