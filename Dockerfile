# Debian закреплён (trixie): от его выпуска зависит мажорная версия FFmpeg
FROM python:3.11-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.9.14 /uv /uvx /usr/local/bin/

WORKDIR /app

# Без рекомендуемых пакетов: они тянут драйверы видеоускорения (mesa и
# др.), которые аудио-потоку не нужны
RUN apt update && \
    apt install -y --no-install-recommends ffmpeg && \
    apt clean && \
    rm -rf /var/lib/apt/lists/*

# Зависимости ставятся отдельным слоем: пересборка только при
# изменении pyproject.toml или uv.lock, а не при каждом коммите
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app/src

EXPOSE 8001 8080

RUN mkdir -p /app/logs /app/cache

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]