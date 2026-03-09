import logging
import signal
import sys
import time

from bot.config import validate_config
from bot.handlers import handle_update
from bot.max_api import MaxBotAPI

logger = logging.getLogger(__name__)

# Флаг для graceful shutdown
_running = True


def _shutdown(signum: int, _frame: object) -> None:
    global _running
    logger.info("Получен сигнал %s, завершаю...", signal.Signals(signum).name)
    _running = False


def main() -> None:
    # Настройка логирования
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Валидация конфигурации
    validate_config()

    # Graceful shutdown
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    api = MaxBotAPI()
    logger.info("Бот запущен. Ожидаю сообщения...")

    try:
        while _running:
            try:
                updates = api.get_updates()
                for update in updates:
                    handle_update(api, update)
            except Exception as exc:
                logger.exception("Ошибка в основном цикле: %s", exc)
                time.sleep(3)  # пауза перед повторной попыткой
    finally:
        api.close()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    main()
