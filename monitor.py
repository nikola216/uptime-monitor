#!/usr/bin/env python3
"""Внешний мониторинг доступности. Запускается GitHub Actions.

Проверка идёт снаружи сервера, оттуда же, откуда приходят пользователи.
Изнутри сервера сбой сети не виден: там всё отвечает, даже когда снаружи
сайт недоступен.

Сообщение уходит только при смене состояния: при первом проваленном запуске
и один раз при восстановлении.
"""

import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

# Один запуск — это уже три попытки подряд, поэтому ждать второго нет смысла:
# расписание GitHub плавает, и второй запуск может случиться через несколько часов.
FAILS_TO_ALERT = 1
ATTEMPTS = 3  # попыток внутри одного запуска
ATTEMPT_TIMEOUT = 15  # секунд на попытку
ATTEMPT_PAUSE = 10  # секунд между попытками
MSK = timezone(timedelta(hours=3))

SITES = [
    ("trainer", "Тренажёр английского", "https://trainer.voxismed.com/api/v1/health"),
    ("sedobno", "Приём заявок СЪЕДОБНО", "https://voxismed.com/api/health"),
]
BOT_ID = "trainer_bot"
BOT_TITLE = "Бот тренажёра в Telegram"


def check_site(url):
    """Сайт доступен, если хотя бы одна попытка вернула 200. Отдаёт и тело ответа."""
    error = ""
    for attempt in range(1, ATTEMPTS + 1):
        started = time.monotonic()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "uptime-monitor"})
            with urllib.request.urlopen(request, timeout=ATTEMPT_TIMEOUT) as response:
                raw = response.read(64 * 1024)
                if response.status == 200:
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        payload = {}
                    return True, f"200 за {time.monotonic() - started:.1f} с", payload
                error = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            error = f"HTTP {exc.code}"
        except Exception as exc:  # таймаут, обрыв соединения, DNS
            error = f"{type(exc).__name__}: {exc}"[:160]
        if attempt < ATTEMPTS:
            time.sleep(ATTEMPT_PAUSE)
    return False, f"{ATTEMPTS} попытки не прошли, последняя ошибка: {error}", {}


def check_bot(health):
    """Состояние бота приложение отдаёт само в /api/v1/health.

    Раньше здесь спрашивался getWebhookInfo, но бот переведён на опрос Telegram:
    вебхука больше нет, и та проверка всегда показывала бы «всё хорошо».
    """
    telegram = (health or {}).get("telegram") or {}
    mode = telegram.get("mode")
    if not mode or mode == "off":
        return None, "опрос выключен"
    if telegram.get("ok"):
        return True, f"опрос жив, обработано апдейтов: {telegram.get('updatesHandled', 0)}"
    reason = telegram.get("lastError") or "последний успешный запрос слишком давно"
    return False, f"опрос не отвечает: {reason}"


def moment(ts):
    return datetime.fromtimestamp(ts, MSK).strftime("%d.%m %H:%M")


def alert_text(title, since, fails, detail, is_bot):
    lines = ["#мониторинг #алерт"]
    if is_bot:
        lines += [f"<b>{title}: не получает сообщения</b>", "", "Приложение работает, но канал до Telegram молчит."]
    else:
        lines += [f"<b>{title}: недоступен снаружи</b>"]
    lines += [
        "",
        f"С {moment(since)} МСК, проверок подряд: {fails}",
        f"Детали: {html.escape(detail)}",
    ]
    return "\n".join(lines)


def recovered_text(title, since, now):
    minutes = max(1, round((now - since) / 60))
    return "\n".join(
        [
            "#мониторинг #восстановлено",
            f"<b>{title}: снова работает</b>",
            "",
            f"Простой около {minutes} мин: с {moment(since)} до {moment(now)} МСК",
        ]
    )


def step(prev, ok, detail, now, title, is_bot):
    """Новое состояние цели и текст сообщения, если состояние сменилось."""
    if ok:
        if prev.get("alerted"):
            return {"fails": 0}, recovered_text(title, prev["down_since"], now)
        return {"fails": 0}, None
    state = dict(prev)
    state["fails"] = state.get("fails", 0) + 1
    state.setdefault("down_since", now)
    state["last_error"] = detail
    if state["fails"] >= FAILS_TO_ALERT and not state.get("alerted"):
        state["alerted"] = True
        return state, alert_text(title, state["down_since"], state["fails"], detail, is_bot)
    return state, None


def send(text):
    if DRY_RUN:
        print("--- сообщение (DRY_RUN, не отправлено) ---\n" + text + "\n---")
        return True
    token = os.environ.get("ALERT_BOT_TOKEN", "")
    chat_id = os.environ.get("ALERT_CHAT_ID", "")
    if not token or not chat_id:
        print("Нет ALERT_BOT_TOKEN или ALERT_CHAT_ID: сообщение не отправлено", file=sys.stderr)
        return False
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    ).encode()
    for attempt in range(3):
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            with urllib.request.urlopen(url, data=data, timeout=20) as response:
                if response.status == 200:
                    return True
        except Exception as exc:
            print(f"Отправка не удалась ({type(exc).__name__})", file=sys.stderr)
        time.sleep(5)
    return False


def main():
    now = int(time.time())
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    results = []
    trainer_health = {}
    for site_id, title, url in SITES:
        ok, detail, payload = check_site(url)
        if site_id == "trainer":
            trainer_health = payload
        results.append((site_id, title, False, ok, detail))

    if trainer_health:
        results.append((BOT_ID, BOT_TITLE, True, *check_bot(trainer_health)))
    else:
        # Сайт не ответил — про бота ничего не известно, состояние не трогаем.
        results.append((BOT_ID, BOT_TITLE, True, None, "сайт не ответил"))

    failed_sends = 0
    for target_id, title, is_bot, ok, detail in results:
        label = "ok" if ok else ("нет данных" if ok is None else "FAIL")
        print(f"{target_id}: {label} — {detail}")
        if ok is None:
            continue
        prev = state.get(target_id, {"fails": 0})
        new, message = step(prev, ok, detail, now, title, is_bot)
        if message and not send(message):
            failed_sends += 1
            # Не отправилось — повторим на следующем запуске.
            new = prev if ok else {**new, "alerted": False}
        state[target_id] = new

    if os.environ.get("TEST_MESSAGE") == "1":
        verdict = {True: "в порядке", False: "не отвечает", None: "нет данных"}
        lines = ["#мониторинг #тест", "<b>Проверка связи: мониторинг работает</b>", ""]
        lines += [f"{title}: {verdict[ok]} ({html.escape(detail)})" for _, title, _, ok, detail in results]
        if not send("\n".join(lines)):
            failed_sends += 1

    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return 1 if failed_sends else 0


if __name__ == "__main__":
    sys.exit(main())
