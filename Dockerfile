# AI Riya voice agent — CPU-only (Silero VAD via onnxruntime, no torch/GPU needed).
FROM python:3.13-slim

# Runtime libs: libgomp1 for onnxruntime; ffmpeg libs are bundled in the PyAV wheel
# but a couple of shared libs help avoid edge cases.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + saved prompts/voices/settings.
COPY server.py bot.py storage.py settings_store.py ./
COPY static/ ./static/
COPY data/ ./data/

# fly.io routes to internal_port; bind all interfaces.
ENV HOST=0.0.0.0 \
    PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "server.py"]
