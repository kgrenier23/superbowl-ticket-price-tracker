FROM python:3.12-slim

# Playwright system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk-bridge2.0-0 libdrm2 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libasound2 libpangocairo-1.0-0 libgtk-3-0 \
    fonts-liberation && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir . && \
    playwright install chromium

COPY . .

# Default: run the scheduler
CMD ["python", "scripts/run_once.py"]
