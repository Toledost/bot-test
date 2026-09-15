FROM python:3.12-slim AS base

# Evita bytecode innecesario y fuerza salida de logs sin buffer (visible en `docker logs`)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencias del sistema mínimas para compilar wheels nativas (numpy/pandas)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Usuario no-root dedicado para ejecutar el bot
RUN groupadd --gid 1000 botuser && \
    useradd --uid 1000 --gid botuser --shell /bin/bash --create-home botuser

COPY --chown=botuser:botuser . .

# Directorios de datos persistentes (montados como volúmenes en docker-compose)
RUN mkdir -p /logs /data && chown -R botuser:botuser /logs /data

USER botuser

ENTRYPOINT ["python", "main.py"]
