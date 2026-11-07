# claude-plugins

Локальный маркетплейс Claude Code с одним плагином — [aiflow](plugins/aiflow/README.md):
очередь задач для фоновых сессий Claude Code (план → реализация → AI-ревью DeepSeek → мерж
в `integration`) с оркестратором, монитором очереди и панелью в браузере.

```bash
claude plugin marketplace add /путь/к/claude-plugins
claude plugin install aiflow@aiflow-local
```

Дальше — раздел «Подготовка репозитория» в README плагина.

Полное описание схемы — [docs/aiflow-workflow.md](docs/aiflow-workflow.md).
