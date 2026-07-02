FROM python:3.9-slim

# Kerakli paketlarni o'rnatamiz
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && sh -c 'echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list' \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Playwright Python kutubxonasini o'rnatamiz
RUN pip install --no-cache-dir -r requirements.txt

# Playwrightga brauzer o'rnatishni buyuramiz (shunchaki patch uchun)
RUN playwright install-deps chromium

CMD ["python", "AI_adbot_logo_version.py"]