FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=180

WORKDIR /app

# Keep dependency installation separate from source copying so ordinary code
# changes reuse the expensive ML dependency layer during local rebuilds.
COPY requirements.txt ./
# Docling and its parser wheels are large. Use a deliberately generous read
# timeout so a slow PyPI transfer does not discard an otherwise valid build.
RUN pip install --no-cache-dir --retries 5 --timeout 180 -r requirements.txt

# Docling imports OpenCV for PDF table/layout processing. The slim Python
# image does not include OpenCV's shared-library runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY . ./

EXPOSE 8000

# The entrypoint waits for Compose services, then replaces itself with the
# normal application entrypoint. main.py remains the single app runtime path.
CMD ["python", "scripts/container_entrypoint.py"]
