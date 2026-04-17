FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg + fonts for drawtext + subtitles
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        fonts-liberation2 \
        fontconfig \
        wget \
        unzip \
        ca-certificates \
    && mkdir -p /usr/share/fonts/truetype/custom \
    && wget -q -O /tmp/Anton.zip "https://fonts.google.com/download?family=Anton" \
    && unzip -o /tmp/Anton.zip -d /usr/share/fonts/truetype/custom/ \
    && fc-cache -f \
    && rm -f /tmp/Anton.zip \
    && apt-get purge -y wget unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

EXPOSE 8000
# Railway sets $PORT; default to 8000 locally
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
