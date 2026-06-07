# 🚀 Production deploy

Чек-лист для деплоя на VPS с доменом и автоматическим SSL.

## Что нужно

1. **VPS** с Ubuntu 22.04 / 24.04, минимум 2 vCPU / 2 GB RAM. Открытые порты `80` и `443`.
2. **Домен**, у которого A-запись указывает на IP твоего VPS.
3. SSH-доступ к VPS.

## Шаг 1. Подготовка VPS

```bash
ssh root@YOUR_SERVER_IP

# обновить систему
apt update && apt upgrade -y

# поставить docker + git
curl -fsSL https://get.docker.com | sh
apt install -y git
```

## Шаг 2. Настроить домен

В панели регистратора (reg.ru / Namecheap / etc) создай **A-запись**:

```
Тип: A
Имя: @  (или конкретно: app)
Значение: <IP твоего VPS>
TTL: 3600
```

Подожди 5-15 минут, проверь:
```bash
dig +short A YOUR_DOMAIN
# должен вернуть IP сервера
```

## Шаг 3. Залить код на сервер

```bash
git clone https://github.com/YOU/tg-summarizer.git /opt/tgsumm
cd /opt/tgsumm
```

(если git ещё нет — скопируй файлы через `scp -r`).

## Шаг 4. Настроить .env

```bash
cp .env.example .env
nano .env
```

**Обязательно поменять:**

- `TG_API_ID`, `TG_API_HASH` — с my.telegram.org
- `CONTROL_BOT_TOKEN`, `CONTROL_BOT_USERNAME` — у @BotFather
- `ADMIN_USER_ID` — твой Telegram user_id
- `INVITE_CODE` — придумай свой, не дефолтный
- `SESSION_ENCRYPTION_KEY` — сгенерировать:
  ```bash
  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- `WEB_SECRET_KEY` — сгенерировать:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(48))"
  ```
- `POSTGRES_PASSWORD` — длинный случайный пароль
- `DEEPSEEK_API_KEY` или `GEMINI_API_KEY` — твой ключ
- `PUBLIC_BASE_URL=https://YOUR_DOMAIN`

## Шаг 5. Указать домен для Caddy

Создай файл `.env.caddy`:

```bash
echo "DOMAIN=YOUR_DOMAIN" >> .env
```

(Caddyfile читает `{$DOMAIN}` из окружения compose, поэтому достаточно положить в основной `.env`.)

## Шаг 6. Запустить

```bash
docker compose -f docker-compose.prod.yml up --build -d
docker compose -f docker-compose.prod.yml logs -f
```

Caddy сам получит Let's Encrypt сертификат при первом обращении к домену. Это занимает 10-30 секунд.

## Шаг 7. Проверка

- Открой `https://YOUR_DOMAIN` — должен открыться лендинг.
- Открой бот @YOUR_BOT_USERNAME → `/start` → должен предложить регистрацию через сайт.
- Зарегистрируйся через сайт.

## Обновление кода

```bash
cd /opt/tgsumm
git pull
docker compose -f docker-compose.prod.yml up --build -d
```

## Бэкап БД

```bash
docker compose -f docker-compose.prod.yml exec db pg_dump -U tg tg | gzip > backup-$(date +%F).sql.gz
```

## Логи

```bash
docker compose -f docker-compose.prod.yml logs app -f --tail 200
docker compose -f docker-compose.prod.yml logs caddy -f --tail 50
```

## Откат

```bash
docker compose -f docker-compose.prod.yml down
git checkout <previous-commit>
docker compose -f docker-compose.prod.yml up --build -d
```

## Безопасность

- Закрой firewall на сервере: разреши только `22, 80, 443`.
  ```bash
  ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw enable
  ```
- Поменяй SSH на ключи, выключи парольный логин.
- Регулярно обновляй систему (`unattended-upgrades`).
- Бэкапь БД — там лежат **зашифрованные сессии всех твоих пользователей**, потеря = логаут всех.
- НИ В КОЕМ СЛУЧАЕ не коммить `.env` в git.
