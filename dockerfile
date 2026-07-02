# Python 3.9 versiyasidan foydalanamiz
FROM python:3.9-slim

# Playwright uchun kerakli tizim kutubxonalarini o'rnatamiz
RUN apt-get update && apt-get install -y \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Ishchi papkani belgilaymiz
WORKDIR /app

# Fayllarni konteynerga ko'chiramiz
COPY . .

# Kutubxonalarni o'rnatamiz
RUN pip install --no-cache-dir -r requirements.txt

# Playwright brauzerini o'rnatamiz
RUN playwright install chromium && playwright install-deps chromium

# Botni ishga tushiramiz
CMD ["python", "AI_adbot_logo_version.py"]