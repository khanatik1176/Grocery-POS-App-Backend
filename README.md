# Grocery POS API

Django REST Framework API. Postgres comes from Neon when `DATABASE_URL` is set. Until then the app uses a local SQLite file so you can boot the server.

## Setup

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

This repo is linked to the Neon project `misty-meadow-29588810`, branch `production`. `neon deploy` writes `DATABASE_URL` (pooled, used by the API) and `DATABASE_URL_UNPOOLED` (direct, used by migrations) into `.env`. Comment both out only if you want the local SQLite fallback.

```bash
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

`0.0.0.0` lets a phone or emulator reach the API. Health check:

```bash
curl http://127.0.0.1:8000/api/health/
```

A connected database returns `{"status":"ok","database":"ok","engine":"postgresql"}`. With the SQLite fallback, `engine` is `sqlite`.

## Stack

- Django 6.1
- Django REST Framework
- Postgres via `psycopg` and `dj-database-url`
- `django-cors-headers` so Expo web can call the API during development
