import os
import re
import time
import json
import requests
import threading
from datetime import datetime, timedelta
import http.server
import socketserver

TOKEN = os.getenv("BOT_TOKEN", "8627499773:AAFmzhJGlJ9gymzTPqKgKJO60QFBsEhO6tQ")
CHATS_FILE = "chats.json"
CHECK_INTERVAL_SEC = 1800  # Проверка каждые 30 минут

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

# ponytail: кэш расписания на 10 минут, чтобы не дергать виджет слишком часто
CAL_CACHE = {}
CAL_CACHE_TIME = {}

def fetch_mlbl_games(comp_id: int) -> list:
    now_ts = time.time()
    if comp_id in CAL_CACHE and now_ts - CAL_CACHE_TIME.get(comp_id, 0) < 600:
        return CAL_CACHE[comp_id]

    url = f"https://reg.infobasket.su/Widget/Calendar/{comp_id}"
    try:
        html = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).text
    except Exception:
        return CAL_CACHE.get(comp_id, [])

    pattern = re.compile(
        r'<tr[^>]*class=["\']game["\'][^>]*id=["\']game-(\d+)["\'].*?'
        r'<td[^>]*class=["\']date["\'][^>]*>\s*([0-9.]+)\s*</td>.*?'
        r'<td[^>]*class=["\']time["\'][^>]*>\s*([0-9:]+)\s*</td>.*?'
        r'<td[^>]*class=["\']arena["\'][^>]*>\s*([^<]+)\s*</td>.*?'
        r'<a[^>]*class=["\']team["\'][^>]*>([^<(]+).*?'
        r'<a[^>]*class=["\']team["\'][^>]*>([^<(]+)',
        re.DOTALL
    )

    games = []
    for m in pattern.findall(html):
        gid, d_str, t_str, arena, t1, t2 = m
        try:
            dt = datetime.strptime(f"{d_str.strip()} {t_str.strip()}", "%d.%m.%Y %H:%M")
        except Exception:
            continue
        games.append({
            "id": int(gid),
            "datetime": dt,
            "date_str": d_str.strip(),
            "time_str": t_str.strip(),
            "arena": arena.strip(),
            "team1": t1.strip(),
            "team2": t2.strip()
        })

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
            res.append({**g, "is_home": is_t1, "form": form})
    return sorted(res, key=lambda x: x["datetime"])

def auto_find_team(team_name: str, preferred_comp_name: str = ""):
    """Ищет команду среди всех дивизионов МЛБЛ."""
    q = team_name.lower().strip()
    p_div = preferred_comp_name.lower().strip()

    # Сначала проверяем предпочитаемый дивизион, если указан
    candidates = []
    for c in COMPETITIONS:
        if p_div and p_div not in c["name"].lower():
            continue
        games = fetch_mlbl_games(c["id"])
        team_matches = filter_team_games(games, q)
        if team_matches:
            # Найдена команда
            exact_name = team_matches[0]["team1"] if q in team_matches[0]["team1"].lower() else team_matches[0]["team2"]
            candidates.append((c["id"], c["name"], exact_name))

    if candidates:
        return candidates[0]

    # Если с уточнением дивизиона не нашли, ищем по всем
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
    except Exception:
        pass
    return None

def delete_msg(chat_id: int | str, message_id: int):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=5
        )
    except Exception:
        pass

def delete_later(chat_id: int | str, message_ids: list, delay: float = 1.5):
    def _worker():
        time.sleep(delay)
        for mid in message_ids:
            if mid:
                delete_msg(chat_id, mid)
    threading.Thread(target=_worker, daemon=True).start()

def reminder_loop():
    """Фоновый поток: проверяет игры ЗАВТРА и шлет напоминание за 1 день."""
    time.sleep(5)
    while True:
        try:
            today = datetime.now().date()
            tomorrow = today + timedelta(days=1)
            cfg = load_chats_config()
            changed = False

            for chat_id, info in cfg.items():
                team = info.get("team")
                comp_id = info.get("comp_id")
                if not team or not comp_id:
                    continue

                reminded_ids = info.setdefault("reminded_ids", [])
                games = filter_team_games(fetch_mlbl_games(comp_id), team)

                for g in games:
                    # Если игра завтра и о ней еще не напоминали
                    if g["datetime"].date() == tomorrow and g["id"] not in reminded_ids:
                        send_msg(chat_id, f"🔔 *Внимание! Завтра игра:*\n\n{format_card(g, 'Завтра матч', info.get('division'))}")
                        reminded_ids.append(g["id"])
                        changed = True

            if changed:
                save_chats_config(cfg)
        except Exception:
            pass
        time.sleep(CHECK_INTERVAL_SEC)

def handle_game_query(chat_id: str, query: str = ""):
    cfg = load_chats_config().get(chat_id)
    if not cfg or not cfg.get("team"):
        send_msg(chat_id, "⚠️ Этот чат еще не привязан к команде.\nАдминистратор должен отправить: `/set НазваниеКоманды`")
        return

    team = cfg["team"]
    comp_id = cfg.get("comp_id")
    games = filter_team_games(fetch_mlbl_games(comp_id), team)
    now = datetime.now()

    # 1. Поиск по конкретной дате (например: /game 15.10 или /game 04.10)
    if query:
        matched = [g for g in games if query in g["date_str"]]
        if matched:
            for g in matched:
                send_msg(chat_id, format_card(g, f"Матч на {g['date_str']}", cfg.get("division")))
        else:
            send_msg(chat_id, f"На дату `{query}` игр для команды *{team}* не найдено.")
        return

    # 2. Поиск ближайшей будущей игры
    upcoming = [g for g in games if g["datetime"] >= now]
    if upcoming:
        send_msg(chat_id, format_card(upcoming[0], "Ближайшая игра", cfg.get("division")))
    else:
        send_msg(chat_id, f"Предстоящих назначенных игр для команды *{team}* пока нет.")

def start_render_web_server():
    """Фоновый HTTP-сервер для прохождения проверки порта на Render.com."""
    port = int(os.getenv("PORT", "10000"))
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

def run_bot():
    print("МЛБЛ Бот запущен...")
    threading.Thread(target=start_render_web_server, daemon=True).start()
    threading.Thread(target=reminder_loop, daemon=True).start()

    offset = 0
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35
            ).json()

            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("channel_post")
                if not msg or "text" not in msg:
                    continue

                chat_id = str(msg["chat"]["id"])
                user_id = msg.get("from", {}).get("id", 0)
                is_private = msg.get("chat", {}).get("type") == "private"
                raw_text = msg["text"].strip()
                cmd = raw_text.split("@")[0].lower()

                cfg = load_chats_config()

                # --- 1. СПРАВКА И ПОМОЩЬ ---
                if cmd in ["/start", "/help"]:
                    if is_private:
                        send_msg(chat_id, (
                            "👋 Привет! Я бот расписания лиги МЛБЛ Москва (moscow.ilovebasket.ru).\n\n"
                            "🏀 *Как подключить к вашей команде:*\n"
                            "1. Добавьте меня в чат вашей команды.\n"
                            "2. Администратор чата должен отправить:\n"
                            "`/set НазваниеКоманды`\n"
                            "*(или с дивизионом: `/set Название | Дивизион`)*\n\n"
                            "Бот автоматически найдет команду, привяжет её и будет присылать напоминания за 1 день до игры!"
                        ))
                    else:
                        if chat_id in cfg and cfg[chat_id].get("team"):
                            info = cfg[chat_id]
                            send_msg(chat_id, (
                                f"🏀 Этот чат настроен на команду: *{info['team']}* ({info.get('division', '')}).\n\n"
                                f"• `/game` — ближайшая игра и цвет формы\n"
                                f"• `/game 15.10` — игра на конкретную дату\n"
                                f"• `/schedule` — полное расписание на 3 месяца\n"
                                f"• Бот сам напомнит о матче за день до игры!"
                            ))
                        else:
                            send_msg(chat_id, "👋 Привет! Чтобы настроить бота, администратор чата должен отправить:\n`/set НазваниеКоманды`")
                    continue

                # --- 2. НАСТРОЙКА КОМАНДЫ (ТОЛЬКО ДЛЯ АДМИНИСТРАТОРОВ) ---
                if cmd.startswith(("/set", "/team")):
                    if not is_private and not is_group_admin(chat_id, user_id):
                        w_id = send_msg(chat_id, "⛔️ Только администратор этой группы может настраивать или менять команду.")
                        delete_later(chat_id, [w_id, msg.get("message_id")], delay=1.5)
                        continue

                    prefix = "/team" if cmd.startswith("/team") else "/set"
                    args = raw_text[len(prefix):].strip()
                    if not args:
                        send_msg(chat_id, "Укажите команду:\n`/set НазваниеКоманды`\nили:\n`/set НазваниеКоманды | Дивизион`")
                        continue

                    if "|" in args:
                        team_in, div_in = [x.strip() for x in args.split("|", 1)]
                    else:
                        team_in, div_in = args, ""

                    # Автоматический поиск команды в МЛБЛ
                    found = auto_find_team(team_in, div_in)
                    if not found:
                        send_msg(chat_id, f"❌ Команда `{team_in}` не найдена в расписании МЛБЛ Москва. Проверьте правильность написания названия на moscow.ilovebasket.ru.")
                        continue

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
                        f"• За 1 день до каждого матча сюда придёт автоматическое напоминание.\n"
                        f"• В любой момент можно написать `/game` (или `/game ДД.ММ`) и `/schedule`."
                    ))
                    continue

                # --- 2.1 ОТВЯЗКА КОМАНДЫ (ТОЛЬКО ДЛЯ АДМИНИСТРАТОРОВ) ---
                if cmd in ["/unset", "/reset"]:
                    if not is_private and not is_group_admin(chat_id, user_id):
                        w_id = send_msg(chat_id, "⛔️ Только администратор этой группы может отвязать команду.")
                        delete_later(chat_id, [w_id, msg.get("message_id")], delay=1.5)
                        continue

                    if chat_id in cfg:
                        old_team = cfg[chat_id].get("team", "")
                        del cfg[chat_id]
                        save_chats_config(cfg)
                        send_msg(chat_id, f"🗑 Привязка к команде *{old_team}* удалена.")
                    else:
                        send_msg(chat_id, "ℹ️ К этому чату не привязана ни одна команда.")
                    continue

                # --- 3. ЗАПРОС ИГРЫ (ДОСТУПНО ВСЕМ) ---
                if cmd.startswith("/game"):
                    arg = raw_text[5:].strip()
                    handle_game_query(chat_id, arg)
                    continue

                if any(c in cmd for c in ["игра", "форма"]):
                    handle_game_query(chat_id, "")
                    continue

                # --- 4. РАСПИСАНИЕ НА ВЕСЬ СЕЗОН ---
                if cmd.startswith(("/schedule", "/calendar")):
                    info = cfg.get(chat_id)
                    if not info or not info.get("team"):
                        send_msg(chat_id, "⚠️ Чат еще не привязан к команде. Отправьте `/set НазваниеКоманды`.")
                        continue

                    games = filter_team_games(fetch_mlbl_games(info["comp_id"]), info["team"])
                    if not games:
                        send_msg(chat_id, f"Расписание для *{info['team']}* пока не опубликовано.")
                        continue

                    lines = [f"📅 *Календарь матчей {info['team']} ({info.get('division', '')}):*\n"]
                    for g in games:
                        opp = g["team2"] if info["team"].lower() in g["team1"].lower() else g["team1"]
                        lines.append(f"• *{g['date_str']}* в *{g['time_str']}* — vs {opp} ({g['form']})")
                    send_msg(chat_id, "\n".join(lines))
                    continue

        except Exception:
            time.sleep(3)

if __name__ == "__main__":
    run_bot()
