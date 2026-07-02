# Microsoft'ning tayyor Playwright tasviridan foydalanamiz
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Fayllarni ko'chiramiz
COPY . .

# Kutubxonalarni o'rnatamiz
RUN pip install --no-cache-dir -r requirements.txt

# Brauzerlar allaqachon ichida bo'lgani uchun alohida install shart emas
# Shunchaki botni ishga tushiramiz
CMD ["python", "AI_adbot_logo_version.py"]