import os
import sys
import re
import time
import json
import socket
import requests
import threading
from datetime import datetime, timezone, timedelta

# Настраиваем UTF-8 для вывода эмодзи на любых платформах (Windows cp1251 / Termux)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except Exception:
        try:
            enc = getattr(sys.stdout, "encoding", "utf-8") or "utf-8"
            cleaned = [str(a).encode(enc, errors="replace").decode(enc, errors="replace") for a in args]
            print(*cleaned, **kwargs)
        except Exception:
            pass

MOSCOW_TZ = timezone(timedelta(hours=3))

def get_moscow_now() -> datetime:
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)
import http.server
import socketserver

# Принудительно используем IPv4 для Android / Termux (избегаем Errno 101 Network is unreachable)
try:
    import urllib3.util.connection as urllib3_cn
    urllib3_cn.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass

def get_bot_token():
    t = os.getenv("BOT_TOKEN")
    if not t and os.path.exists(".env"):
        try:
            with open(".env", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("BOT_TOKEN="):
                        t = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    if not t:
        try:
            t = input("Введите токен Telegram-бота (BOT_TOKEN): ").strip()
            if t:
                with open(".env", "w", encoding="utf-8") as f:
                    f.write(f"BOT_TOKEN={t}\n")
        except Exception:
            pass
    return t or ""

TOKEN = get_bot_token()
CHATS_FILE = "chats.json"
CHECK_INTERVAL_SEC = 1800

COMPETITIONS = [
    {"id": 142698, "name": "Элита"},
    {"id": 142702, "name": "Претенденты (1)"},
    {"id": 142703, "name": "Претенденты (2)"},
    {"id": 142704, "name": "Лига Развития (1)"},
    {"id": 142705, "name": "Лига Развития (2)"},
    {"id": 142706, "name": "Лига Развития (3)"},
    {"id": 142707, "name": "Лига Развития (4)"},
    {"id": 142699, "name": "Lite"},
    {"id": 142708, "name": "Лига развития (4). Дружба"},
    {"id": 142717, "name": "W-Элита"},
    {"id": 142718, "name": "W-MLBL (2)"},
    {"id": 142711, "name": "Ветераны 35+ (1)"},
    {"id": 142712, "name": "Ветераны 35+ (2)"},
    {"id": 142714, "name": "Ветераны 40+ Дружба"},
    {"id": 142715, "name": "Ветераны 45+ Дружба"},
    {"id": 142716, "name": "Ветераны 50+"},
    {"id": 142719, "name": "МЛБЛ-Дети U18 (1)"},
    {"id": 142720, "name": "МЛБЛ-Дети U18 (2)"},
    {"id": 142721, "name": "МЛБЛ-Дети U16 (1)"},
    {"id": 142722, "name": "МЛБЛ-Дети U16 (2)"},
    {"id": 142723, "name": "МЛБЛ-Дети U14 (1)"},
    {"id": 142724, "name": "МЛБЛ-Дети U14 (2)"},
    {"id": 142725, "name": "МЛБЛ-Дети U12 (1)"}
]

def load_chats_config() -> dict:
    if os.path.exists(CHATS_FILE):
        try:
            with open(CHATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_chats_config(data: dict):
    with open(CHATS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_group_admin(chat_id: str | int, user_id: int) -> bool:
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getChatMember",
            params={"chat_id": chat_id, "user_id": user_id},
            timeout=5
        ).json()
        if r.get("ok"):
            status = r.get("result", {}).get("status")
            return status in ["creator", "administrator"]
    except Exception:
        pass
    return False

CAL_CACHE = {}
CAL_CACHE_TIME = {}

def fetch_mlbl_games(comp_id: int) -> list:
    """Загружает все матчи дивизиона (и завершенные, и запланированные на 3 месяца) через JSON API."""
    now_ts = time.time()
    if comp_id in CAL_CACHE and now_ts - CAL_CACHE_TIME.get(comp_id, 0) < 600:
        return CAL_CACHE[comp_id]

    url = f"https://reg.infobasket.su/Comp/GetCalendar/?comps={comp_id}&format=json"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://moscow.ilovebasket.ru/"}
    try:
        raw_list = requests.get(url, headers=headers, timeout=10).json()
    except Exception:
        return CAL_CACHE.get(comp_id, [])

    if not isinstance(raw_list, list):
        return CAL_CACHE.get(comp_id, [])

    games = []
    for g in raw_list:
        if not isinstance(g, dict):
            continue
        gid = g.get("GameID")
        d_str = g.get("GameDate", "")
        t_str = g.get("GameTime", "00:00") or "00:00"
        arena = g.get("ArenaRu") or "Зал уточняется"
        t1 = g.get("ShortTeamNameAru") or g.get("TeamNameAru") or ""
        t2 = g.get("ShortTeamNameBru") or g.get("TeamNameBru") or ""
        status = g.get("GameStatus", 0)  # 0 = запланирована, 1 = сыграна

        try:
            dt = datetime.strptime(f"{d_str} {t_str}", "%d.%m.%Y %H:%M")
        except Exception:
            dt = datetime.min

        games.append({
            "id": gid,
            "datetime": dt,
            "date_str": d_str,
            "time_str": t_str,
            "arena": arena,
            "team1": t1.strip(),
            "team2": t2.strip(),
            "status": status,
            "score_a": g.get("ScoreA", 0),
            "score_b": g.get("ScoreB", 0),
            "div_name": g.get("LeagueNameRu", "")
        })

    games.sort(key=lambda x: x["datetime"])
    CAL_CACHE[comp_id] = games
    CAL_CACHE_TIME[comp_id] = now_ts
    return games

def filter_team_games(games: list, team_name: str) -> list:
    q = team_name.lower().strip()
    res = []
    for g in games:
        is_t1 = q in g["team1"].lower()
        is_t2 = q in g["team2"].lower()
        if is_t1 or is_t2:
            form = "⚪️ СВЕТЛАЯ" if is_t1 else "⚫️ ЧЁРНАЯ"
            opp = g["team2"] if is_t1 else g["team1"]
            res.append({**g, "is_home": is_t1, "form": form, "opponent": opp})
    return res

def auto_find_team(team_name: str, preferred_comp_name: str = ""):
    q = team_name.lower().strip()
    p_div = preferred_comp_name.lower().strip()

    candidates = []
    for c in COMPETITIONS:
        if p_div and p_div not in c["name"].lower():
            continue
        games = fetch_mlbl_games(c["id"])
        team_matches = filter_team_games(games, q)
        if team_matches:
            exact_name = team_matches[0]["team1"] if q in team_matches[0]["team1"].lower() else team_matches[0]["team2"]
            candidates.append((c["id"], c["name"], exact_name))

    if candidates:
        return candidates[0]
    if p_div:
        return auto_find_team(team_name, "")
    return None

def format_card(g: dict, title: str = "Матч", div_name: str = "") -> str:
    div_str = f"🏆 *Дивизион:* {div_name}\n" if div_name else ""
    return (
        f"🏀 *{title}*\n\n"
        f"⚔️ *Игра:* {g['team1']} — {g['team2']}\n"
        f"{div_str}"
        f"📅 *Когда:* {g['date_str']} в {g['time_str']}\n"
        f"📍 *Зал:* {g['arena']}\n"
        f"👕 *Форма:* {g['form']}"
    )

def send_msg(chat_id: int | str, text: str) -> int | None:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        ).json()
        if r.get("ok"):
            return r["result"]["message_id"]
        # Резерв без Markdown, если в тексте были спецсимволы
        r2 = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10
        ).json()
        if r2.get("ok"):
            return r2["result"]["message_id"]
    except Exception:
        pass
    return None

def delete_msg(chat_id: int | str, message_id: int):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/deleteMessage",
            json={"chat_id": chat_id, "message_id": int(message_id)},
            timeout=5
        ).json()
        if not r.get("ok"):
            safe_print(f"⚠️ Ошибка удаления {message_id}: {r.get('description')}")
    except Exception as e:
        safe_print(f"⚠️ Сбой сети при удалении {message_id}: {e}")

def delete_later(chat_id: int | str, message_ids: list, delay: float = 3.0):
    """Удаляет список сообщений через delay секунд (в фоне, чтобы не блокировать бота)."""
    def _worker():
        time.sleep(delay)
        valid_ids = []
        for m in message_ids:
            if m:
                try:
                    valid_ids.append(int(m))
                except (ValueError, TypeError):
                    pass
        if not valid_ids:
            return

        valid_ids = sorted(list(set(valid_ids)))

        # 1. Пробуем удалить всё разом через deleteMessages (Bot API 7.0+)
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/deleteMessages",
                json={"chat_id": chat_id, "message_ids": valid_ids},
                timeout=5
            ).json()
            if r.get("ok"):
                return
        except Exception:
            pass

        # 2. Поштучное удаление с небольшой паузой
        for mid in valid_ids:
            delete_msg(chat_id, mid)
            time.sleep(0.3)
    threading.Thread(target=_worker, daemon=True).start()

def reminder_loop():
    """Фоновый поток: проверяет игры ЗАВТРА и шлет напоминание за 1 день."""
    time.sleep(5)
    while True:
        try:
            today = get_moscow_now().date()
            tomorrow = today + timedelta(days=1)
            cfg = load_chats_config()
            changed = False

            for chat_id, info in cfg.items():
                if not isinstance(info, dict):
                    continue
                team = info.get("team")
                comp_id = info.get("comp_id")
                if not team or not comp_id:
                    continue

                reminded_ids = info.setdefault("reminded_ids", [])
                games = filter_team_games(fetch_mlbl_games(comp_id), team)

                for g in games:
                    # Напоминаем только о запланированных играх на завтра
                    if g["status"] == 0 and g["datetime"].date() == tomorrow and g["id"] not in reminded_ids:
                        send_msg(chat_id, f"🔔 *Внимание! Завтра игра:*\n\n{format_card(g, 'Завтра матч', info.get('division'))}")
                        reminded_ids.append(g["id"])
                        changed = True

            if changed:
                save_chats_config(cfg)
        except Exception as e:
            print(f"⚠️ Ошибка в reminder_loop: {e}")
        time.sleep(CHECK_INTERVAL_SEC)

def handle_game_query(chat_id: str, query: str = ""):
    cfg = load_chats_config().get(chat_id)
    now = get_moscow_now()

    # 1. Запрос с параметром (дата или название команды)
    if query:
        # Проверяем, похож ли запрос на дату (например, 11.10 или 18.10.2026)
        is_date = bool(re.search(r"^\d{1,2}[\.\/]\d{1,2}", query.strip()))
        if is_date:
            if not cfg or not cfg.get("team"):
                send_msg(chat_id, "⚠️ Этот чат еще не привязан к команде.\nАдминистратор должен отправить: `/set НазваниеКоманды`")
                return
            team = cfg["team"]
            comp_id = cfg.get("comp_id")
            games = filter_team_games(fetch_mlbl_games(comp_id), team) if comp_id else []
            matched = [g for g in games if query.strip() in g["date_str"]]
            if matched:
                for g in matched:
                    t_label = "Запланированный матч" if g["status"] == 0 else f"Счёт: {g['score_a']}:{g['score_b']}"
                    send_msg(chat_id, format_card(g, f"{t_label} на {g['date_str']}", cfg.get("division")))
            else:
                send_msg(chat_id, f"На дату `{query.strip()}` игр для команды *{team}* не найдено.")
            return

        # Запрос названия команды (например: /game ЦСКА или /game ЦСКА | Дивизион)
        team_in = query.strip()
        div_in = ""
        if "|" in team_in:
            team_in, div_in = [x.strip() for x in team_in.split("|", 1)]

        found = auto_find_team(team_in, div_in)
        if found:
            comp_id, comp_name, exact_team_name = found
            games = filter_team_games(fetch_mlbl_games(comp_id), exact_team_name)
            upcoming = [g for g in games if g["status"] == 0 and g["datetime"] >= now]
            if upcoming:
                send_msg(chat_id, format_card(upcoming[0], "Ближайшая игра", comp_name))
            else:
                future_or_today = [g for g in games if g["status"] == 0]
                if future_or_today:
                    send_msg(chat_id, format_card(future_or_today[0], "Ближайшая игра", comp_name))
                else:
                    send_msg(chat_id, f"Все запланированные матчи для команды *{exact_team_name}* ({comp_name}) уже завершены.")
            return

        # Если команда не найдена через автопоиск, но чат привязан, проверяем совпадение в зале/дате
        if cfg and cfg.get("team") and cfg.get("comp_id"):
            team = cfg["team"]
            games = filter_team_games(fetch_mlbl_games(cfg["comp_id"]), team)
            matched = [g for g in games if team_in.lower() in g["date_str"].lower() or team_in.lower() in g["arena"].lower()]
            if matched:
                for g in matched:
                    t_label = "Запланированный матч" if g["status"] == 0 else f"Счёт: {g['score_a']}:{g['score_b']}"
                    send_msg(chat_id, format_card(g, f"{t_label}", cfg.get("division")))
                return

        send_msg(chat_id, f"❌ Команда или матч `{query}` не найдены в расписании МЛБЛ.")
        return

    # 2. Запрос без параметра — по умолчанию команда текущего чата
    if not cfg or not cfg.get("team"):
        send_msg(chat_id, "⚠️ Этот чат еще не привязан к команде.\nАдминистратор должен отправить: `/set НазваниеКоманды`")
        return

    team = cfg["team"]
    comp_id = cfg.get("comp_id")
    if not comp_id:
        send_msg(chat_id, "⚠️ Ошибка конфигурации команды. Попробуйте привязать заново через `/set`.")
        return

    games = filter_team_games(fetch_mlbl_games(comp_id), team)
    upcoming = [g for g in games if g["status"] == 0 and g["datetime"] >= now]
    if upcoming:
        send_msg(chat_id, format_card(upcoming[0], "Ближайшая игра", cfg.get("division")))
    else:
        future_or_today = [g for g in games if g["status"] == 0]
        if future_or_today:
            send_msg(chat_id, format_card(future_or_today[0], "Ближайшая игра", cfg.get("division")))
        else:
            send_msg(chat_id, f"Все запланированные матчи для команды *{team}* уже завершены.")

def handle_schedule_query(chat_id: str, query: str = ""):
    cfg = load_chats_config().get(chat_id)
    target_team = ""
    comp_id = None
    div_name = ""

    if query:
        team_in = query.strip()
        div_in = ""
        if "|" in team_in:
            team_in, div_in = [x.strip() for x in team_in.split("|", 1)]
        found = auto_find_team(team_in, div_in)
        if not found:
            send_msg(chat_id, f"❌ Команда `{query}` не найдена в расписании МЛБЛ.")
            return
        comp_id, div_name, target_team = found
    elif cfg and cfg.get("team"):
        target_team = cfg["team"]
        comp_id = cfg.get("comp_id")
        div_name = cfg.get("division", "")
    else:
        send_msg(chat_id, "⚠️ Чат еще не привязан к команде. Отправьте `/set НазваниеКоманды` или `/schedule НазваниеКоманды`.")
        return

    if not comp_id:
        send_msg(chat_id, "⚠️ Ошибка конфигурации команды. Попробуйте привязать заново через `/set`.")
        return

    games = filter_team_games(fetch_mlbl_games(comp_id), target_team)
    if not games:
        send_msg(chat_id, f"Расписание для *{target_team}* пока не найдено.")
        return

    upcoming = [g for g in games if g["status"] == 0]
    finished = [g for g in games if g["status"] == 1]

    lines = [f"📅 *Календарь матчей {target_team} ({div_name}):*\n"]
    if upcoming:
        lines.append("*⏳ Запланированные матчи:*")
        for g in upcoming:
            lines.append(f"• *{g['date_str']}* в *{g['time_str']}* — vs {g['opponent']} ({g['form']}) | {g['arena']}")

    if finished:
        lines.append("\n*🏁 Завершённые матчи:*")
        for g in finished:
            lines.append(f"• {g['date_str']} — {g['team1']} {g['score_a']}:{g['score_b']} {g['team2']}")

    send_msg(chat_id, "\n".join(lines))

def start_render_web_server():
    port = int(os.getenv("PORT", "10001"))
    class QuietHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"MLBL Bot is running!")
        def log_message(self, format, *args):
            return
    try:
        server = socketserver.TCPServer(("", port), QuietHandler)
        server.serve_forever()
    except Exception:
        pass

def process_message(msg: dict):
    """Обрабатывает одно входящее сообщение Telegram для MLBL бота."""
    try:
        if not msg or not isinstance(msg, dict) or "text" not in msg:
            return

        raw_text = msg["text"].strip()

        # Игнорируем любые обычные сообщения в чате — реагируем СТРОГО на слэш-команды
        if not raw_text.startswith("/"):
            return

        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not chat_id:
            return

        user_id = msg.get("from", {}).get("id", 0)
        is_private = msg.get("chat", {}).get("type") == "private"
        is_anon_admin = (msg.get("sender_chat", {}).get("id") == msg.get("chat", {}).get("id"))

        parts = raw_text.split(None, 1)
        cmd_token = parts[0]
        args = parts[1].strip() if len(parts) > 1 else ""
        cmd = cmd_token.split("@")[0].lower()

        safe_print(f"🤖 [{chat_id}] {raw_text}")
        cfg = load_chats_config()

        # --- 1. СПРАВКА ---
        if cmd in ["/start", "/help"]:
            if is_private:
                send_msg(chat_id, (
                    "👋 Привет! Я бот расписания лиги МЛБЛ Москва.\n\n"
                    "🏀 *Как подключить:*\n"
                    "1. Добавьте меня в чат вашей команды.\n"
                    "2. Администратор чата должен отправить:\n"
                    "`/set НазваниеКоманды`\n\n"
                    "Бот сам найдет дивизион команды и будет напоминать о матчах накануне!"
                ))
            else:
                if chat_id in cfg and cfg[chat_id].get("team"):
                    info = cfg[chat_id]
                    send_msg(chat_id, (
                        f"🏀 Чат привязан к: *{info['team']}* ({info.get('division', '')})\n\n"
                        f"• `/game` — ближайший матч и цвет формы\n"
                        f"• `/game 11.10` — матч на дату\n"
                        f"• `/schedule` — календарь матчей на сезон\n"
                        f"• Напоминания приходят автоматически за 1 день до игры."
                    ))
                else:
                    send_msg(chat_id, "👋 Чтобы настроить бота, администратор должен отправить:\n`/set НазваниеКоманды`")
            return

        # --- 2. НАСТРОЙКА КОМАНДЫ (ТОЛЬКО ДЛЯ АДМИНИСТРАТОРОВ) ---
        if cmd in ["/set", "/team"]:
            if not is_private and not is_anon_admin and not is_group_admin(chat_id, user_id):
                w_id = send_msg(chat_id, "⛔️ Только администратор этой группы может настраивать команду.")
                delete_later(chat_id, [w_id, msg.get("message_id")], delay=2.5)
                return

            if not args:
                send_msg(chat_id, "Укажите команду:\n`/set НазваниеКоманды`")
                return

            if "|" in args:
                team_in, div_in = [x.strip() for x in args.split("|", 1)]
            else:
                team_in, div_in = args, ""

            found = auto_find_team(team_in, div_in)
            if not found:
                send_msg(chat_id, f"❌ Команда `{team_in}` не найдена в расписании МЛБЛ. Проверьте название на moscow.ilovebasket.ru.")
                return

            comp_id, comp_name, exact_team_name = found
            cfg[chat_id] = {
                "team": exact_team_name,
                "comp_id": comp_id,
                "division": comp_name,
                "reminded_ids": []
            }
            save_chats_config(cfg)

            send_msg(chat_id, (
                f"✅ Чат успешно привязан!\n\n"
                f"🏀 Команда: *{exact_team_name}*\n"
                f"🏆 Дивизион: *{comp_name}*\n\n"
                f"• За 1 день до каждого матча придёт автоматическое напоминание.\n"
                f"• Напишите `/game` (или `/game ДД.ММ`) и `/schedule`, чтобы посмотреть календарь."
            ))
            return

        # --- 2.1 ОТВЯЗКА ---
        if cmd in ["/unset", "/reset"]:
            if not is_private and not is_anon_admin and not is_group_admin(chat_id, user_id):
                w_id = send_msg(chat_id, "⛔️ Только администратор этой группы может отвязать команду.")
                delete_later(chat_id, [w_id, msg.get("message_id")], delay=2.5)
                return

            if chat_id in cfg:
                old_team = cfg[chat_id].get("team", "")
                del cfg[chat_id]
                save_chats_config(cfg)
                send_msg(chat_id, f"🗑 Привязка к команде *{old_team}* удалена.")
            else:
                send_msg(chat_id, "ℹ️ К этому чату не привязана ни одна команда.")
            return

        # --- 3. ЗАПРОС ИГРЫ ---
        if cmd in ["/game", "/mlbl"]:
            handle_game_query(chat_id, args)
            return

        # --- 4. КАЛЕНДАРЬ НА СЕЗОН ---
        if cmd in ["/schedule", "/calendar"]:
            handle_schedule_query(chat_id, args)
            return

        # --- 5. ДРУЖЕСТВЕННЫЙ ОТВЕТ НА ABL КОМАНДЫ ---
        if cmd in ["/abl", "/next"]:
            send_msg(chat_id, "🏀 Этот бот предназначен для лиги МЛБЛ Москва.\nИспользуйте команду `/game` для просмотра расписания матчей МЛБЛ.")
            return

    except Exception as e:
        safe_print(f"⚠️ Ошибка обработки сообщения: {e}")

def run_bot():
    if not TOKEN:
        safe_print("❌ ОШИБКА: Токен бота не найден! Проверьте файл .env")
        return
    safe_print(f"МЛБЛ Бот запущен (токен: {TOKEN[:10]}***, длина {len(TOKEN)} симв.)...")
    threading.Thread(target=start_render_web_server, daemon=True).start()
    threading.Thread(target=reminder_loop, daemon=True).start()

    # Сбрасываем любые чужие вебхуки и спам
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook", json={"drop_pending_updates": True}, timeout=10)
    except Exception:
        pass

    offset = 0
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35
            ).json()

            if not r.get("ok"):
                desc = r.get("description", "Unknown error")
                safe_print(f"⚠️ Ошибка Telegram API: {desc}")
                time.sleep(3)
                continue

            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("channel_post")
                if msg:
                    process_message(msg)

        except Exception as e:
            safe_print(f"❌ Ошибка в цикле: {e}")
            time.sleep(3)

if __name__ == "__main__":
    run_bot()
