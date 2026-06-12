# FITNESS 24 → iOS Calendar

Автоматизация для подписного календаря iOS с выбранными групповыми занятиями FITNESS 24 Приморский.

## Что делает

- Проверяет расписание FITNESS 24 Приморский.
- Ищет текущую и следующую недели.
- Добавляет только занятия из `allowed_titles` в `config.yaml`.
- Исключает платные, детские/юниорские/молодежные и отмененные занятия.
- Создает `public/fitness24-primorskiy.ics`.
- Ставит напоминание за 3 часа.

## Выбранные занятия

- ЗДОРОВАЯ СПИНА
- ZUMBA
- PILATES
- OUTDOOR «RUNNING»
- STRETCH MOBILITY
- FIT BALL
- SPINNING START
- ДЫХАТЕЛЬНАЯ ГИМНАСТИКА
- BELLY DANCE
- STRETCHING
- СУСТАВНАЯ ГИМНАСТИКА
- DANCE MIX.

## Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python generate_calendar.py --config config.yaml
```

После запуска файл календаря появится здесь:

```text
public/fitness24-primorskiy.ics
```

## Запуск в GitHub Actions

1. Создайте новый публичный или приватный GitHub-репозиторий.
2. Загрузите туда эти файлы.
3. Включите GitHub Pages: **Settings → Pages → Source → GitHub Actions**.
4. Запустите workflow вручную: **Actions → Update FITNESS 24 calendar → Run workflow**.
5. После успешного запуска файл будет доступен по адресу вида:

```text
https://<github-username>.github.io/<repo-name>/fitness24-primorskiy.ics
```

## Подписка в iOS

На iPhone:

1. Откройте **Calendar / Календарь**.
2. Нажмите **Calendars / Календари**.
3. Нажмите **Add Calendar / Добавить календарь**.
4. Выберите **Add Subscription Calendar / Добавить подписной календарь**.
5. Вставьте URL `.ics`.
6. Назовите календарь, например `FITNESS 24`.

## Часовой пояс

По умолчанию события создаются в часовом поясе клуба: `Europe/Moscow`.

Если нужно, чтобы iPhone всегда показывал время ровно как на сайте, даже когда телефон находится в другом часовом поясе, установите в `config.yaml`:

```yaml
floating_times: true
```

## Настройки

Все основные параметры находятся в `config.yaml`:

- `allowed_titles` — список занятий;
- `reminder_hours_before` — напоминание за N часов;
- `lookahead_weeks` — сколько недель вперед проверять;
- `exclude.paid` — исключать платные;
- `exclude.children` — исключать детские/юниорские/молодежные;
- `exclude.cancelled` — исключать отмененные.
