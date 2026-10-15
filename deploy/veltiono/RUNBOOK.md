# Quickly — ранбук (Veltiono, outreach.veltiono.eu)

## 1. Что это

Один контейнер приложения Quickly и Postgres, работающие на одном VPS за существующим nginx или
Traefik. Ежедневный зашифрованный бэкап уходит в Cloudflare R2, алерты и ежедневный «жив» — в
Telegram ops-чат. Масштабировать нельзя: планировщик живёт внутри процесса приложения, поэтому
`app` всегда ровно один экземпляр.

## 2. Подготовка сервера

- Пакет `age`: `apt install -y age`. На Debian 11 и Ubuntu 20.04 пакета нет или он старый: скачать
  релиз `age-v1.2.1-linux-amd64.tar.gz` с github.com/FiloSottile/age/releases и положить `age` и
  `age-keygen` в `/usr/local/bin`.
- Файрвол. Сначала посмотреть, что уже слушает на сервере, иначе `deny incoming` отрежет живые
  сервисы (и вас самих):
  ```bash
  ufw status verbose        # уже ли включён и какие правила есть
  ss -tlnp                  # порты, на которых что-то слушает снаружи (не 127.0.0.1 / ::1)
  ```
  Разрешить порт SSH (**реальный**, из `ss -tlnp` или `sshd -T | grep ^port`: нестандартный порт
  легко забыть) и порты всех остальных нужных сервисов, и только потом закрывать по умолчанию:
  ```bash
  ufw allow <ssh-порт>/tcp
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw allow <порт каждого другого сервиса из ss -tlnp>/tcp
  ufw default deny incoming
  ufw enable
  ```
  IPv6: в `/etc/default/ufw` должно быть `IPV6=yes`, иначе правила не действуют на IPv6-адреса
  (а DNS-запись AAAA может на них вести); после правки `ufw reload`. Не закрывайте текущую
  SSH-сессию, пока не проверили вход второй сессией. Опубликованные порты Docker обходят ufw: наружу
  публиковать только то, что задумано (в оверлее nginx порт приложения привязан к `127.0.0.1`).
- DNS: запись A (и AAAA, если есть IPv6) `outreach.veltiono.eu` → IP сервера.

## 3. Код и `.env`

```bash
git clone --depth 1 https://github.com/Travel2Space/quickly /opt/quickly-src
mkdir -p /opt/quickly && cp -r /opt/quickly-src/deploy/veltiono/. /opt/quickly/
cd /opt/quickly
cp .env.example .env && cp ops.env.example ops.env && chmod 600 .env ops.env
```

Обновить скрипты позже: `git -C /opt/quickly-src pull && cp -r /opt/quickly-src/deploy/veltiono/. /opt/quickly/`.
`.env`, `ops.env` и `state/` при этом не затираются: их нет в репозитории.

`.env` — настройки приложения и compose: файл целиком попадает в контейнер `app`. Секреты бэкапа и
алертов (R2, Telegram, публичный ключ age) лежат отдельно, в `ops.env`: их читают только скрипты на
хосте, приложению они не нужны. Скрипты без `ops.env` не запускаются.

Генерация значений:

```bash
openssl rand -hex 24                          # POSTGRES_PASSWORD
openssl rand -hex 32                          # QUICKLY_SECRET_KEY
openssl rand -base64 32 | tr '+/' '-_'        # QUICKLY_ENCRYPTION_KEY
```

**В менеджер паролей сохранить:** `QUICKLY_SECRET_KEY`, `QUICKLY_ENCRYPTION_KEY`, `POSTGRES_PASSWORD`.
Без ключа шифрования пароли ящиков из бэкапа не расшифровать — их придётся ввести заново.

## 4. Образ

Первый релиз: тег `v0.1.0` на `main` форка, после влития всех PR спеки 1:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

Workflow `Image (GHCR)` проверит образ (старт против Postgres и `/health`) и опубликует
`ghcr.io/travel2space/quickly:v0.1.0`. Это значение `QUICKLY_IMAGE_TAG` в `.env`.

Пакет `ghcr.io/travel2space/quickly` сделать публичным: GitHub → Travel2Space → Packages → quickly →
Package settings → Change visibility. Либо оставить приватным и выполнить
`docker login ghcr.io -u <пользователь>` с токеном, у которого есть только право `read:packages`.

## 5. Вход в интерфейс (Google OAuth)

Google Cloud Console → OAuth client (тип Web), authorized redirect URI:
`https://outreach.veltiono.eu/oauth/app/google/callback`. Client ID и secret — в `.env`
(`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`).

## 6. Прокси и запуск

Сначала выбрать прокси-оверлей: переменная `COMPOSE_FILE` в `.env`.

- **nginx на хосте:** `COMPOSE_FILE=docker-compose.yml:docker-compose.nginx.yml`. Установка сайта:
  шапка файла `nginx/outreach.veltiono.eu.conf` (копия в `sites-available`, `nginx -t`, `certbot --nginx -d outreach.veltiono.eu --redirect`).
  Порт в `.env` (`QUICKLY_PORT`, по умолчанию 5050) и в nginx-конфиге должен совпадать.
- **Traefik на хосте:** `COMPOSE_FILE=docker-compose.yml:docker-compose.traefik.yml`. Проверить
  `TRAEFIK_NETWORK`, `TRAEFIK_ENTRYPOINT`, `TRAEFIK_CERTRESOLVER` под свой Traefik.

Запуск и проверка:

```bash
docker compose up -d
docker compose ps          # app должен быть healthy
curl -s https://outreach.veltiono.eu/health    # → {"ok":true}
```

## 7. Pre-send проверка

`PRESEND_SECRET` в `.env` — то же значение, что `QUICKLY_PRESEND_SECRET` в Vercel (Production).
После правки `.env`: `docker compose up -d app`.

## 8. Бэкапы (R2)

- В Cloudflare R2 создать API token с правом Object Read & Write **только на бакет** `veltiono-backups`.
- `BACKUP_AGE_PUBLIC_KEY` и `R2_*` — в `ops.env`. Ключ — тот же публичный ключ, что в GitHub secret `BACKUP_AGE_PUBLIC_KEY`
  веб-бэкапа. Приватный ключ на сервер не попадает никогда.
- Каждый прогон пишет в свою папку `quickly/<YYYY-MM-DD>T<HHMMSS>Z/`: сначала `dump.pgc.age`, потом
  проверка размера, и **последним** `manifest.json` — это отметка «бэкап полный». Если что-то упало,
  объекты этой папки удаляются. Восстановление и учебное восстановление берут самую свежую папку, в
  которой есть `manifest.json`. Старые папки удаляются по дате из имени (`RETENTION_DAYS`); сбой
  очистки не считается провалом бэкапа (в лог пишется `prune failed`).
- Первый прогон вручную: `bash scripts/backup.sh`, затем проверить в R2 появление `quickly/<папка>/`
  с файлами `dump.pgc.age` и `manifest.json`.

## 9. Telegram (ops-чат)

1. Создать чат, добавить бота.
2. Узнать `chat_id`: написать в чат сообщение и открыть
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Вписать `TELEGRAM_BOT_TOKEN` и `TELEGRAM_OPS_CHAT_ID` в `ops.env`.
4. Проверка: `bash scripts/heartbeat.sh` — в чат должно прийти сообщение «жив».

## 10. Cron и logrotate

```bash
cp cron/quickly /etc/cron.d/quickly
cp cron/quickly-logrotate /etc/logrotate.d/quickly
```

Расписание считает время сервера в UTC (`timedatectl` → Time zone: UTC). Бэкап — 03:30 UTC,
проверка здоровья — каждые 5 минут, «жив» — 06:00 UTC (09:00 по Афинам летом, 08:00 зимой).

## 11. Обновление

```bash
bash scripts/update.sh v0.1.1
```

Скрипт сам делает бэкап, подтягивает образ, переключает тег и ждёт `healthy`. Если новая версия не
поднялась (или любой шаг упал) — откатывается на прежний тег и шлёт сообщение в Telegram с именем
бэкапа, сделанного перед обновлением. **Откат образа не отменяет миграции БД новой версии:** если
после отката приложение или данные ведут себя странно, восстановите этот бэкап
(`bash scripts/restore-prod.sh <папка>`). Откат вручную:
`bash scripts/update.sh <прежний тег>`. `latest` скрипт не принимает.

## 12. Учебное восстановление (раз в месяц)

```bash
bash scripts/restore-drill.sh
```

Скрипт попросит вставить приватный ключ age из менеджера паролей (ввод скрыт). Дамп идёт потоком
R2 → age → `pg_restore` во временный Postgres: ни расшифрованного дампа, ни файла с ключом на диске
нет (ключ живёт только в переменной оболочки и передаётся в age через канал).
Дождаться строки `drill ok`. Тестовый контейнер удаляется сам.

## 13. Восстановление в бою

```bash
bash scripts/restore-prod.sh [YYYY-MM-DD или имя папки]
```

Без аргумента берётся самый свежий полный бэкап (с `manifest.json`); дата выбирает самую свежую папку
этого дня, имя папки — именно её. Скрипт попросит напечатать `RESTORE` и вставить приватный
ключ age (ввод скрыт, не сохраняется). Расшифрованный дамп и ключ на диск не пишутся. Порядок:

1. **Проверка бэкапа.** Поток R2 → age → `pg_restore -l`. Неверный ключ или битый дамп останавливают
   скрипт здесь: ничего не тронуто, приложение работает.
2. Приложение останавливается, с живой базы снимается **страховочный дамп**, зашифрованный
   `BACKUP_AGE_PUBLIC_KEY`: `state/pre-restore-<UTC>.pgc.age`.
3. Бэкап разворачивается в **новую** базу `<имя>_restore` рядом с живой.
4. Только после успеха: живая база удаляется, `<имя>_restore` переименовывается в неё, приложение
   стартует. Путь к страховочному дампу приходит в Telegram.

При любой ошибке до шага 4 скрипт удаляет `<имя>_restore`, запускает приложение на нетронутой базе и
шлёт сообщение в Telegram. Сбой посреди шага 4 (редкий) помечается отдельным предупреждением:
тогда восстановите живую базу из страховочного дампа. Нужен тот же `QUICKLY_ENCRYPTION_KEY`, что был
при бэкапе. Страховочные дампы сами не удаляются: чистить `state/` вручную.

## 14. Алерты и что делать

- **«не отвечает»** → `docker compose ps`, затем `docker compose logs --tail 100 app`.
- **«бэкап не удался»** → `/var/log/quickly-backup.log`.
- **«диск заполнен»** → освободить место (логи Docker, старые образы: `docker image prune`
  без флага `-a`), при необходимости увеличить диск.
- **«TLS-сертификат истекает»** → проверить certbot (nginx) или resolver Traefik.
- **Нет утреннего «жив»** → сервер недоступен целиком: зайти в панель хостинга.
