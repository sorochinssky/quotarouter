# hermes-web-router

Квотно-осознанный маршрутизатор поиска и вытяжки страниц для Hermes Agent.

Задача простая: у каждого вендора есть бесплатный месячный лимит. Хочется тратить его
первым, знать остаток **до** запроса и не получать `402 Insufficient Balance` в логах.
Hermes сам так не умеет: `web.search_backend` — одно имя, а встроенный keyless-rescue
срабатывает уже после провала, на каждый вызов заново.

Плагин подставляет в Hermes **один** бэкенд `router`. Модель по-прежнему зовёт
`web_search` / `web_extract`, но запрос идёт по цепочке ступеней, а выбор ступени
делается по **остатку квоты из персистентного ledger'а**, а не по факту ошибки.

## Зачем своё, если есть hermes-seek / hermes-sift

| Что нужно | seek/sift | этот плагин |
|---|---|---|
| Цепочка провайдеров | да | да |
| Учёт **остатка** до вызова | нет, реакция на 402 | да, ledger + `probe` |
| Пауза переживает рестарт | нет, в памяти | да, JSON на диске |
| Возврат в строй сам, по дате сброса окна | 24 ч после 402 | по `billingPeriodEnd` вендора |
| Проверка, что «успех» не пустой | нет | да: EMPTY / BAD_PAGE / заглушка |
| Extract без ключа первым (экономия кредитов) | — | да, Jina Reader |
| Аудит: почему выбрана эта ступень | частично | `routes.jsonl` + `router route` |

## Установка

```bash
hermes plugins install /root/repos/hermes-web-router --name web-router
hermes config set web.search_backend router
hermes config set web.extract_backend router
hermes config set web.keyless_rescue false     # свой резерв последней ступенью, а не снаружи
# плагины грузятся при старте:
systemctl --user restart hermes-gateway        # или свой способ рестарта
```

Ключи вендоров — обычные переменные окружения Hermes (`~/.hermes/.env`):
`EXA_API_KEY`, `FIRECRAWL_API_KEY`, `DEEPSEEK_API_KEY`, опционально `TAVILY_API_KEY`,
`KEENABLE_API_KEY`, `JINA_API_KEY`.

## Порядок ступеней

По умолчанию:

```
search : keenable → exa → tavily → firecrawl → deepseek
extract: jina → keenable → firecrawl → tavily → exa → deepseek
```

Вендор без ключа просто пропускается. Переопределяется одной переменной:

```bash
ROUTER_SEARCH_ORDER=exa,firecrawl,deepseek
ROUTER_EXTRACT_ORDER=jina,firecrawl,exa
```

Почему такой порядок по умолчанию: exa отдаёт ~1 400 поисков за $10, firecrawl — общий
пул 1 000 кредитов (2 кредита/поиск, 1 кредит/страница), jina бесплатна и потому идёт
первой на вытяжке. Замеры — `docs/vendor-probes.md`.

## Эксплуатация

```bash
python -m router.cli status          # что осталось у каждого вендора
python -m router.cli probe           # спросить вендоров про остаток и записать в ledger
python -m router.cli route           # кто обслужит следующий вызов и почему не остальные
python -m router.cli selftest        # один живой поиск + одна вытяжка, end-to-end
python -m router.cli disable exa --reason "тест ключа"
python -m router.cli enable exa
python -m router.cli reset firecrawl # сбросить локальный учёт после смены тарифа
```

`probe` стоит гонять по крону: тогда первый поиск дня не узнаёт про пустой пул из 402.

```cron
17 */6 * * * cd /root/repos/hermes-web-router && . /root/.hermes/.env && /usr/bin/python3 -m router.cli probe --quiet
```

## Политика ошибок

| Что прислал вендор | Код | Что делает router |
|---|---|---|
| 402 / `Insufficient Balance` / `NO_MORE_CREDITS` | `quota` | парковка до даты сброса окна (или 24 ч) |
| 401 / 403 (не квота) | `auth` | парковка 24 ч — нужен человек |
| 429 | `throttle` | пауза по `Retry-After`, иначе 60 с с удвоением |
| 5xx, timeout, обрыв | `transient` | пауза 60 с, переход к следующей ступени |
| 200, но пусто | `empty` | следующая ступень, ступень заносится в BAD_PAGE |
| Extract: все URL с ошибкой | `bad_page` | следующая ступень |
| Extract: 403/404/410 самой страницы | — | **не** ошибка стадии: отдаём странице её причину |

Последняя строка важна: `403 from origin` — это ответ сайта, а не сбой вендора. Тратить
кредиты второй ступени на страницу, которая закрыта от всех, бессмысленно.

## Тестирование

```bash
python -m pytest -q            # 78 тестов, офлайн, без сети — CI
python -m pytest -m live -q    # живые вызовы вендоров, тратят квоту
```

Четыре уровня:

1. `tests/test_errors.py`, `test_ledger.py` — классификация ответов и учёт, на таблицах.
2. `tests/test_stages.py`, `test_routing.py` — адаптеры и цепь на `MockTransport`.
3. `tests/test_adversarial.py` — записанные ответы (`tests/fixtures/`) и враждебные случаи:
   `success: true` без строк, HTTP 200 с телом ошибки, обрыв соединения, смена схемы ответа.
4. `tests/test_live.py` — реальные вендоры, помечены `live`, по умолчанию не бегут.

## Файлы состояния

```
~/.hermes/state/web-router/ledger.json    квоты, паузы, периоды
~/.hermes/state/web-router/routes.jsonl   одна JSON-строка на вызов: кто обслужил и кто почему не смог
```

Оба файла пишутся атомарно (`os.replace`) и переживают рестарт gateway.
