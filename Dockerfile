FROM python:3.12-slim

LABEL org.opencontainers.image.title="LocalShare" \
      org.opencontainers.image.description="Self-hosted LAN media server" \
      org.opencontainers.image.source="https://github.com/Hexanol777/LocalShare"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN addgroup --system localshare \
 && adduser  --system --ingroup localshare --no-create-home localshare

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p instance uploads \
 && chown -R localshare:localshare /app

USER localshare

EXPOSE 80

ENTRYPOINT ["python", "app.py"]
CMD []