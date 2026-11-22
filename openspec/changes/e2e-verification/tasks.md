# Tasks: e2e-проверка в настоящем Hermes

## 1. Стенд

- [x] 1.1 `tests/e2e/harness.py`: собирает временный `HERMES_HOME` (config.yaml, .env,
  plugins/web-router с симлинком на пакет). Проверка: каталог существует, `config.yaml`
  содержит `search_backend: router`.
- [x] 1.2 Дочерний скрипт-раннер: шаги 1–5 выполняются в отдельном процессе с
  `HERMES_HOME=<temp>` и `PYTHONPATH=/usr/local/lib/hermes-agent`. Проверка: шаг 1 видит
  провайдера `router` в реестре Hermes.
- [x] 1.3 Шаг изоляции: `~/.hermes/state/web-router` не создаётся живым вызовом. Проверка:
  до/после прогона mtime живого каталога не меняется (или каталога нет).

## 2. Сценарии

- [x] 2.1 `search_tool`: `success: true`, ≥1 результат, `data.backend` = `router:<name>`.
- [x] 2.2 `extract_tool`: `results[0].content` непустой.
- [x] 2.3 Учёт: `ledger.json` и `routes.jsonl` появились под изолированным home, в журнале
  есть `served_by`.
- [x] 2.4 Деградация: без ключей и с `ROUTER_KEYLESS_LAST_RESORT=false` инструмент отдаёт
  `success: false` с именами ступеней, без трейсбека.

## 3. Документация

- [x] 3.1 `docs/testing.md`: лестница уровней (unit / mock-цепь / записанные ответы /
  live-адаптеры / e2e), для каждого — что проверяет, чем платит, что считается
  доказательством, команда запуска.
- [x] 3.2 README: раздел «Тестирование» ссылается на `docs/testing.md`, добавлены команды
  e2e.

## 4. Гейты

- [x] 4.1 `pyproject.toml`: маркеры `e2e`, оба (`live`, `e2e`) исключены из `addopts`;
  `pytest -q` остаётся офлайн и зелёным.
- [x] 4.2 `pytest -q` → 78+ passed, ни одного сетевого вызова.
- [x] 4.3 `python -m pytest -m e2e -q` → прогон стенда, отчёт `e2e-report.json`, код 0.
- [x] 4.4 Прогон e2e на живых ключах: поиск и вытяжка обслужены реальными вендорами,
  отчёт приложен в задаче/событии.
