# ton-signal-bot
Telegram bot that every 30 minutes sends a report of TON tokens with the strongest short-term momentum. Pure Python script on GitHub Actions: no AI, no paid APIs. Data: GeckoTerminal public API, Cointelegraph/CoinDesk RSS.
Commands: /report, /gainers_1h|24h|7d, /losers_1h|24h|7d, /help (answered by the `commands` workflow via long polling). Report verdict % = historical hit rate (>= +5% within 6h) per score bucket from history.json; heuristic "(оценка)" capped at 60% until 20 samples.
Secrets: `TG_BOT_TOKEN`, `TG_CHAT_ID`. Not financial advice.
