FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Сначала манифест зависимостей — слой кешируется, пока не изменится pyproject.toml
COPY pyproject.toml ./

# Исходники пакета ДО установки: иначе setuptools находит ноль пакетов
# и editable-установка создаёт пустой mapping (работает лишь за счёт CWD в sys.path).
COPY app ./app

RUN pip install --upgrade pip && pip install -e ".[dev]"

# Остальные файлы (тесты, alembic — появятся позже)
COPY . .

EXPOSE 8000

# --proxy-headers + forwarded-allow-ips: без них request.client.host за Caddy/nginx
# равен адресу прокси, и лимит «3 запроса / 10 мин на IP» превращается в глобальный
# лимит на весь продукт (четвёртый пользователь получает 429).
# IP доверенных прокси задаются переменной FORWARDED_ALLOW_IPS.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-127.0.0.1}"]
