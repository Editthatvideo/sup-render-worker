FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DENO_INSTALL=/usr/local

# ffmpeg + fonts + deno (yt-dlp JS runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        fonts-liberation2 \
        fontconfig \
        wget \
        curl \
        unzip \
        ca-certificates \
    && mkdir -p /usr/share/fonts/truetype/custom \
    && wget -q -O /usr/share/fonts/truetype/custom/Anton-Regular.ttf "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf" \
    && fc-cache -f \
    && curl -fsSL https://deno.land/install.sh | sh \
    && apt-get purge -y wget curl unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

EXPOSE 8000
# Railway sets $PORT; default to 8000 locally
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
