FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Papkani aniq belgilaymiz va unga yozish uchun ruxsatni ta'minlaymiz
ENV PLAYWRIGHT_BROWSERS_PATH=/app/ms-playwright
RUN mkdir -p /app/ms-playwright

RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium && playwright install-deps chromium

CMD ["python", "AI_adbot_logo_version.py"]