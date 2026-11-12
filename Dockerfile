FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=Europe/Madrid
COPY pyproject.toml README.md ./
COPY newsbot ./newsbot
RUN pip install --no-cache-dir . && useradd --create-home bot && mkdir -p /app/data && chown bot /app/data
COPY config.toml ./
USER bot
CMD ["python", "-m", "newsbot.bot"]
