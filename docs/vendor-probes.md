# Замеры вендоров, 2026-11-22

Снимались живыми вызовами на прод-ключах из `/root/.hermes/.env`. Нужны, чтобы расчёт
квот в ledger и юнит-цены в стадиях опирались на факты, а не на прайс-листы.

## Exa (search)

```
POST https://api.exa.ai/search
{"query": "Nous Research Hermes Agent", "numResults": 1, "contents": {"highlights": true}}
-> costDollars: {"total": 0.007, "search": {"neural": 0.007}, "contents": {"highlights": 0.0}}
```

- План: $10/мес, оплаченный кредит (не «1000 бесплатных запросов»).
- Цена поиска не зависит от числа результатов до 10: $0.007 за neural-поиск + highlights.
- Отсюда ~1 400 поисков/мес при $10. Подсветки идут бесплатно.
- Нет публичного endpoint свободного остатка; `MONTHLY_CREDIT_USD = 10.0` в ledger
  считается локально, сброс — 1-е число месяца.

## Firecrawl (search + scrape)

```
GET https://api.firecrawl.dev/v2/team/credit-usage
-> {"success": true, "data": {"remainingCredits": 989, "planCredits": 1000,
     "billingPeriodStart": "2026-10-30T07:28:51.871Z",
     "billingPeriodEnd":   "2026-11-30T07:28:51.871Z"}}
```

- `POST /v2/search` → `creditsUsed: 2` (2 кредита за поиск, до 5 результатов).
- `POST /v2/scrape` → 1 кредит за успешную страницу, неуспешная не тарифицируется.
- Итого: 1 000 кредитов = ~500 поисков ИЛИ 1 000 страниц; пул общий.
- Окно сбрасывается **30-го числа**, не 1-го. `probe_quota()` парсит `billingPeriodEnd` в
  ключ периода `firecrawl:YYYY-MM-DD` — ledger сам откроет новое окно.
- Это единственный вендор с API остатка → единственный, у кого `source: api`, а не `local`.

## Jina Reader (extract)

- `https://r.jina.ai/<url>` без ключа: 20 запросов/мин с одного IP.
- С `JINA_API_KEY` — ключевой лимит, платный тариф.
- Отдаёт `Title:` / `URL Source:` / `Markdown Content:`; пустая страница и JS-стена
  распознаются как заглушка (`looks_like_stub`), после чего цепь уходит на следующий вендор.

## DeepSeek (search + fetch)

```
GET https://api.deepseek.com/user/balance
-> {"is_available": false, "balance_infos": [{"total_balance": "-0.06", ...}]}
```

- $0.75 / 1 000 успешных запросов = $0.00075 за вызов (в ~9 раз дешевле Exa).
- Баланс отрицательный: каждое обращение → 402 `Insufficient Balance`.
- Стратегия: ступень остаётся в списке, но `probe_quota()` видит `is_available: false`
  и **паркует стадию** вместо вызова. После пополнения `router enable deepseek` или
  ближайший `probe` вернёт её в строй.

## Что из этого следует для порядка

- **search**: exa (пул $10, дёшево при 1 400 вызовах) → firecrawl (общий пул, 2 кредита/поиск)
  → deepseek (за баланс). Keenable/Tavily в списке, но без ключей пропускаются.
- **extract**: jina (бесплатно, без ключа) → firecrawl (1 кредит/страница) → exa (дороже).
  Jina первым экономит самый дефицитный пул — кредиты Firecrawl общие с поиском.
