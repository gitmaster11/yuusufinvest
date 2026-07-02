FROM python:3.9-slim

# Kerakli kutubxonalarni root sifatida o'rnatamiz (Docker buni avtomatik qiladi)
RUN apt-get update && apt-get install -y \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libnss3-dev libxss1 libasound2 wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Kutubxonalarni o'rnatamiz
RUN pip install --no-cache-dir -r requirements.txt

# Playwrightni o'rnatamiz (brauzerlar bilan)
# --with-deps bayrog'i kerakli bog'liqliklarni o'zi o'rnatadi
RUN playwright install chromium --with-deps

CMD ["python", "AI_adbot_logo_version.py"]