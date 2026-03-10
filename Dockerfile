FROM python:3.11-slim

# ffmpeg нужен для pydub (нарезка аудио) и pyannote (обработка аудио)
# libsndfile1 нужен для torchaudio/soundfile
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала ставим torch CPU-версию с фиксированными версиями
# (pyannote.audio 3.3.2 несовместим с torchaudio >= 2.5, где убрали AudioMetaData)
RUN pip install --no-cache-dir torch==2.4.1 torchaudio==2.4.1 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ bot/

CMD ["python", "-m", "bot.main"]
