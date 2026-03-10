FROM python:3.11-slim

# ffmpeg нужен для pydub (нарезка аудио) и pyannote (обработка аудио)
# libsndfile1 нужен для torchaudio/soundfile
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала ставим torch CPU-версию (меньше размер, GPU не нужен)
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ bot/

CMD ["python", "-m", "bot.main"]
