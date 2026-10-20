# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system bot && adduser --system --ingroup bot --home /app bot

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=bot:bot main.py ./
RUN mkdir /app/data

USER bot

# The container runtime owns restart policy; do not nest the Termux supervisor.
CMD ["python", "-u", "main.py"]
