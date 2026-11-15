# YTB Conversor — imagem para amd64 e arm64
FROM denoland/deno:bin AS deno

FROM python:3.12-slim

ARG APP_VERSION=dev
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH \
    APP_VERSION=${APP_VERSION} \
    PORT=8080 \
    DATA_DIR=/data \
    HF_HOME=/data/hf

# ffmpeg corta/converte; deno é o motor JavaScript que o yt-dlp usa para o YouTube
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno

RUN useradd -m -u 1000 app \
 && python -m venv /opt/venv \
 && mkdir -p /data \
 && chown -R app:app /opt/venv /data

USER app
COPY --chown=app:app requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY --chown=app:app app/ /app/
COPY --chown=app:app docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/api/health',timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "sh", "/usr/local/bin/docker-entrypoint.sh"]
