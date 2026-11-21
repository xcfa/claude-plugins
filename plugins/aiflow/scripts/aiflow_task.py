"""Задача очереди: что лежит в .aiflow/<id>/task.json.

Код работает с задачей как с обычным dict, а этот тип — опись её ключей: что каждый значит и кто его
пишет. Тест (tests/test_dispatcher.py, TaskSchema) сверяет опись с кодом: ключ, которого здесь нет,
в коде появиться не может, и наоборот.

Почти все ключи, кроме заданных при постановке, служебные: их ставит и снимает диспетчер
(aiflow_dispatcher.Dispatcher) между проходами, чтобы помнить, что он уже сделал.
"""
from __future__ import annotations

from typing import TypedDict

# Переход между стадиями. Первая запись истории — постановка задачи: {"at", "event": "created", "by"}.
HistoryEntry = TypedDict("HistoryEntry", {
    "at": str,
    "from": str,
    "to": str,
    "note": str,   # причина словами, для человека
    "code": str,   # код причины ухода к человеку (aiflow_core.HUMAN_CODES): по нему считает статистика
    "event": str,
    "by": str,
}, total=False)


class SessionEntry(TypedDict, total=False):
    """Фоновая сессия Claude Code одной роли. У роли их бывает несколько: будят сессию иногда новой."""
    id: str                  # короткий id из `claude agents`
    name: str                # "<роль>: <id задачи>" — по нему находят вкладку и сессию после запуска
    started_at: str
    session_id: str | None   # полный id разговора: по нему сессию продолжают (claude --resume)
    cwd: str                 # рабочая копия сессии: продолжать её надо оттуда же
    model: str               # с чем запущена: продолжение с другой моделью теряет кэш разговора
    effort: str
    resumed_at: str          # когда будили последний раз: две минуты после этого её снимку не верим


class Task(TypedDict, total=False):
    # -- задано при постановке (aiflow enqueue)
    id: str                              # он же ветка ai/<id> и папка .aiflow/<id>/
    title: str
    text: str                            # текст задачи от пользователя
    kind: str                            # task | discuss | ask
    items: int                           # сколько независимых мелких пунктов в задаче
    depends_on: list[str]                # после чьего мержа стартует: "<id>" или "<проект>/<id>"
    ui: str | None                       # дизайн для задачи с интерфейсом; переходит к частям при разбиении
    planner_model: str | None            # модель и effort для этой задачи вместо настроек проекта
    planner_effort: str | None
    worker_model: str | None
    worker_effort: str | None
    allowed_protected_paths: list[str]   # защищённые пути, которые человек разрешил этой задаче
    created_at: str
    split_of: str                        # id исходной задачи, если это часть разбиения на несколько PR
    split_into: list[str]                # части, на которые разбили эту задачу (она сама — первая)

    # -- где задача сейчас
    state: str                           # стадия: aiflow_core.STATION и SIDE_STATES
    state_at: str                        # когда в неё пришла; сдвигается и при пробуждении сессии
    history: list[HistoryEntry]          # последние HISTORY_KEEP переходов
    stations: list[int]                  # шаги доски, которые задача прошла (история может быть обрезана)
    pr: int | None
    complexity: str | None               # trivial | simple | complex — оценка планировщика
    plan_approved: bool
    route: str                           # "self" — планировщик делает задачу сам, без исполнителя
    route_note: str                      # что именно он делает сам: строка для панели
    self_executed: bool                  # планировщик сделал сам и ведёт ревью своего PR
    direct: bool                         # без плана: исполнитель работает по указаниям планировщика
    brief: str                           # эти указания
    answer: dict                         # ответ на вопрос (kind: ask): summary, details, at

    # -- сессии
    sessions: dict[str, list[SessionEntry]]  # по ролям: planner, worker
    launching: dict                      # сессия запускается: role, name, at — если запуск оборвётся, её найдут по имени
    usage: dict                          # расход токенов по сессиям и ревьюеру
    waits: dict                          # сессия ждёт ответа человека: sid и её последние слова

    # -- отчёты сессий (aiflow report)
    report_seen: str | None              # rid последнего разобранного отчёта
    report_prev: str | None              # rid перед ним: к нему откатываемся, если GitHub не ответил
    bounced: dict[str, int]              # сколько раз подряд отчёт роли отклонён (не тот PR)
    nudged: dict[str, int]               # сколько раз роли напомнили, что ход кончился без отчёта
    auto_resumed: dict[str, int]         # сколько раз подряд сессию роли возобновили без человека
    limit_hits: dict                     # сколько пауз по лимиту подряд дала одна сессия: sid, n

    # -- план
    plan_check: dict                     # ревью плана другой моделью: phase, runs, pid, результат
    plan_seen_at: str                    # до какого комментария в PR планировщик уже видел обсуждение
    revising: bool                       # планировщика разбудили комментарием, он правит план
    approve_hold: str                    # план одобрен, пока планировщик его дорабатывает: с какого времени ждём

    # -- дополнения пользователя и комментарии в PR
    amendments: list[dict]               # дополнения: n, text, source, author, at, delivered_at
    amend_lost_told: bool                # уже сказали, что дополнение пришло после мержа
    comments_seen_at: str                # до какого комментария PR просмотрен в поиске дополнений
    comments_floor: str                  # комментарии старше — обсуждение плана: ревью кода их не касается

    # -- зависимости
    deps_checked: dict[str, dict]        # последняя проверка зависимости из другого проекта: at, status, note
    deps_published: list[str]            # зависимости, у которых прошли workflow на коммите мержа

    # -- ревью и мерж
    ready_at: str                        # когда PR встал в очередь ревью: по этому порядку его берут
    review_wait: str                     # почему PR ещё ждёт ревью (общие файлы с другим PR): строка для панели
    auto_review: dict                    # ревью запущено самим aiflow, без пробуждения исполнителя: pid, at, head
    review_retried: bool                 # сбой инструмента ревью уже повторяли: второй подряд — к человеку
    files_cache: dict                    # файлы PR на коммите sha: чтобы не спрашивать GitHub каждый проход
    ci: dict                             # сводка проверок CI на последнем коммите
    ci_wake: str                         # коммит, из-за красного CI на котором исполнителя уже будили
    approved_sha: str                    # коммит, который одобрил ревьюер
    approved_at: str
    awaiting_merge: bool                 # одобрен ровно этот коммит и CI не красный: шаг доски «Мерж»

    # -- «нужен человек»
    human_reason: str | None             # что случилось
    human_action: str                    # что сделать (aiflow_core.WHAT_TO_DO)
    human_watch: dict | None             # какой была сессия: заработает сама — задача вернётся в стадию
    retry_requested: bool                # человек нажал «Повторить»
    retry_pending: bool                  # повтор ждёт свободного слота исполнителя
    retry_note: str                      # что сказать сессии при повторе
    path_request: list[str]              # защищённые пути, о которых просила сессия
    path_asked: bool                     # сессия просила защищённый путь: в панели кнопка «Разрешить пути…»

    # -- уведомления и уборка
    notified: list[str]                  # ключи уже отправленных событий: одно и то же второй раз не шлём
    batch_announced: bool                # задача вошла в сообщение «пачка завершена»
    cleaned: bool                        # сессии законченной задачи убраны: следить больше не за чем
