"""MLBL Telegram schedule bot.

The module deliberately keeps its operational state outside the source directory by
default, uses atomic JSON replacements, and never prompts for, persists, or logs
Telegram credentials.
"""

from __future__ import annotations

import html
import http.server
import json
import os
import re
import shutil
import signal
import socket
import socketserver
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import requests

# Keep console output usable on Windows, without making stdout configuration a
# prerequisite for importing or testing this module.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def safe_print(*args: object, **kwargs: object) -> None:
    """Best-effort diagnostic output; callers must never include credentials."""
    try:
        print(*args, **kwargs)
    except Exception:
        pass


MOSCOW_TZ = timezone(timedelta(hours=3))


def get_moscow_now() -> datetime:
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)


# Table of visually similar Cyrillic/Latin characters.
HOMOGLYPHS = str.maketrans({
    "a": "а", "c": "с", "e": "е", "k": "к", "o": "о", "p": "р", "x": "х", "y": "у",
    "A": "а", "B": "в", "C": "с", "E": "е", "H": "н", "K": "к", "M": "м", "O": "о",
    "P": "р", "T": "т", "X": "х", "Y": "у",
    "А": "а", "В": "в", "С": "с", "Е": "е", "Н": "н", "К": "к", "М": "м", "О": "о",
    "Р": "р", "Т": "т", "Х": "х", "У": "у",
})
TRANSLIT_RU_TO_EN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def translit_ru(value: str) -> str:
    return "".join(TRANSLIT_RU_TO_EN.get(char, char) for char in (value or "").lower())


def clean_alnum(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", (value or "").lower().replace("ё", "е"))


def clean_homo(value: str) -> str:
    return clean_alnum(value).translate(HOMOGLYPHS)


def match_text(query: str, target: str) -> bool:
    """Match team names despite punctuation, homoglyphs, and transliteration."""
    if not query or not target:
        return False
    q_raw, t_raw = query.strip(), target.strip()
    if not q_raw or not t_raw:
        return False
    q_low, t_low = q_raw.lower(), t_raw.lower()
    if q_low == t_low:
        return True
    cq, ct = clean_alnum(q_raw), clean_alnum(t_raw)
    if not cq or not ct:
        return False
    if cq == ct:
        return True
    if len(cq) < 3:
        return q_low in t_low.split() or cq in [clean_alnum(word) for word in t_low.split()]
    if q_low in t_low or cq in ct or (len(ct) >= 4 and ct in cq):
        return True
    hq, ht = clean_homo(q_raw), clean_homo(t_raw)
    if hq in ht or (len(ht) >= 4 and ht in hq):
        return True
    tq, tt = clean_alnum(translit_ru(q_raw)), clean_alnum(translit_ru(t_raw))
    if tq in tt or (len(tt) >= 4 and tt in tq):
        return True
    words = [word for word in re.split(r"[^a-zа-я0-9]+", q_low)
             if len(word) >= 2 and word not in ("бк", "bc", "team", "команда")]
    return bool(words) and all(clean_homo(word) in ht or clean_alnum(translit_ru(word)) in tt for word in words)


def parse_team_and_division(text: str) -> tuple[str, str]:
    text = (text or "").strip()
    if not text:
        return "", ""
    if "|" in text:
        team, division = text.split("|", 1)
        return team.strip(), division.strip()
    match = re.match(r"^(.*?)\s*[\(\[](.*?)[\)\]]$", text)
    if match and match.group(1).strip():
        return match.group(1).strip(), match.group(2).strip()
    return text, ""


# Force requests/urllib3 to prefer IPv4 on Android/Termux where broken IPv6 is
# common.  This is deliberately optional.
try:
    import urllib3.util.connection as urllib3_cn
    urllib3_cn.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a small, forgiving .env file without executing it."""
    values: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    continue
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                values[key] = value
    except (OSError, UnicodeError):
        pass
    return values


def load_env_file(path: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Load values from an env file into the process only when unset.

    This function never requests a token interactively and never writes an env
    file.  It is retained as a public compatibility helper.
    """
    target = Path(path) if path else Path(__file__).resolve().parent / ".env"
    values = _read_env_file(target)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(value: str | None, default: int, minimum: int = 1, maximum: int = 86400) -> int:
    try:
        parsed = int(value or default)
        return min(max(parsed, minimum), maximum)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    """Validated runtime configuration read from environment and an optional .env."""

    token: str
    data_dir: Path
    chats_file: Path
    offset_file: Path
    port: int
    check_interval_sec: int
    drop_pending_updates: bool

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        script_dir: Path | None = None,
    ) -> "Settings":
        script_dir = (script_dir or Path(__file__).resolve().parent).resolve()
        file_values = _read_env_file(script_dir / ".env")
        supplied = dict(os.environ if environ is None else environ)
        values = {**file_values, **supplied}  # Explicit environment wins.
        configured_dir = values.get("BOT_DATA_DIR") or values.get("DATA_DIR") or "data"
        data_dir = Path(configured_dir).expanduser()
        if not data_dir.is_absolute():
            data_dir = script_dir / data_dir
        data_dir = data_dir.resolve()
        chats_name = values.get("CHATS_FILE", "chats.json")
        chats_file = Path(chats_name).expanduser()
        if not chats_file.is_absolute():
            chats_file = data_dir / chats_file
        offset_name = values.get("UPDATE_OFFSET_FILE", "update_offset.json")
        offset_file = Path(offset_name).expanduser()
        if not offset_file.is_absolute():
            offset_file = data_dir / offset_file
        return cls(
            token=(values.get("BOT_TOKEN") or "").strip(),
            data_dir=data_dir,
            chats_file=chats_file.resolve(),
            offset_file=offset_file.resolve(),
            port=_positive_int(values.get("PORT"), 10001, 1, 65535),
            check_interval_sec=_positive_int(values.get("CHECK_INTERVAL_SEC"), 1800, 10),
            # Preserve queued updates unless an operator explicitly opts in.
            drop_pending_updates=_as_bool(values.get("DROP_PENDING_UPDATES"), False),
        )


SETTINGS = Settings.from_environment()
TOKEN = SETTINGS.token
CHATS_FILE = str(SETTINGS.chats_file)
OFFSET_FILE = str(SETTINGS.offset_file)
CHECK_INTERVAL_SEC = SETTINGS.check_interval_sec
CONFIG_LOCK = threading.RLock()
OFFSET_LOCK = threading.RLock()
CACHE_LOCK = threading.RLock()
CACHE_REFRESH_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
SERVICE_STARTED = threading.Event()
LAST_POLL_SUCCESS_AT = 0.0
HEALTH_STALE_AFTER_SEC = 90
CACHE_TTL_SEC = 600
CACHE_MAX_STALE_SEC = 3600
SEND_MAX_ATTEMPTS = 3
TELEGRAM_MESSAGE_LIMIT = 4096


def get_bot_token() -> str:
    """Compatibility accessor; unlike older versions it has no interactive path."""
    return TOKEN


def _path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def _write_json_atomic(path: Path, data: Any, keep_backup: bool = True) -> None:
    """Durably replace a JSON file, retaining the last known-good version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        if keep_backup and path.exists():
            backup_tmp = path.with_name(f".{path.name}.bak.tmp")
            with path.open("rb") as source, backup_tmp.open("wb") as destination:
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(backup_tmp, Path(str(path) + ".bak"))
        os.replace(tmp_path, path)
        if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(str(path.parent), os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError, UnicodeError):
        return None


def _migrate_legacy_chats_file() -> None:
    """Copy a historic source-root chats.json into the configured absolute path."""
    target = _path(CHATS_FILE)
    legacy = Path(__file__).resolve().parent / "chats.json"
    if target.exists() or not legacy.exists() or legacy.resolve() == target:
        return
    data = _read_json_object(legacy)
    if data is not None:
        try:
            _write_json_atomic(target, data, keep_backup=False)
        except OSError:
            pass


def load_chats_config() -> dict[str, Any]:
    with CONFIG_LOCK:
        _migrate_legacy_chats_file()
        path = _path(CHATS_FILE)
        data = _read_json_object(path)
        if data is not None:
            return data
        backup = _read_json_object(Path(str(path) + ".bak"))
        if backup is not None:
            # Restore from a verified backup without replacing that backup with
            # the corrupt primary content.
            try:
                _write_json_atomic(path, backup, keep_backup=False)
            except OSError:
                pass
            safe_print("Configuration primary file was corrupt; recovered its backup.")
            return backup
        return {}


def save_chats_config(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ValueError("chat configuration must be a JSON object")
    with CONFIG_LOCK:
        try:
            _write_json_atomic(_path(CHATS_FILE), data)
        except (OSError, TypeError, ValueError) as exc:
            safe_print(f"Could not save chat configuration: {exc}")


def load_update_offset() -> int:
    with OFFSET_LOCK:
        data = _read_json_object(_path(OFFSET_FILE))
        if not data:
            return 0
        try:
            return max(0, int(data.get("offset", 0)))
        except (TypeError, ValueError):
            return 0


def save_update_offset(offset: int) -> None:
    try:
        normalized = max(0, int(offset))
    except (TypeError, ValueError):
        return
    with OFFSET_LOCK:
        try:
            _write_json_atomic(_path(OFFSET_FILE), {"offset": normalized, "updated_at": int(time.time())})
        except (OSError, TypeError, ValueError) as exc:
            safe_print(f"Could not persist Telegram update offset: {exc}")


def update_chats_config(mutator: Any) -> Any:
    """Run a state mutation beneath one lock and commit it atomically.

    Every persistent chat-state writer must use this helper so a reminder update,
    a command handler, and a delivery failure cannot overwrite one another with
    independently loaded snapshots.
    """
    with CONFIG_LOCK:
        config = load_chats_config()
        result = mutator(config)
        save_chats_config(config)
        return result


def disable_chat(chat_id: int | str, reason: str = "") -> None:
    def mutate(config: dict[str, Any]) -> bool:
        key = str(chat_id)
        if key in config and isinstance(config[key], dict) and config[key].get("enabled", True):
            config[key]["enabled"] = False
            return True
        return False

    if update_chats_config(mutate):
        safe_print(f"Chat {chat_id} disabled after Telegram delivery failure ({reason[:80]}).")


def is_group_admin(chat_id: str | int, user_id: int) -> bool:
    try:
        result = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getChatMember",
            params={"chat_id": chat_id, "user_id": user_id},
            timeout=5,
        ).json()
        return bool(result.get("ok") and result.get("result", {}).get("status") in ("creator", "administrator"))
    except Exception:
        return False


CAL_CACHE: dict[int, list[dict[str, Any]]] = {}
CAL_CACHE_TIME: dict[int, float] = {}


def _cached_games(comp_id: int, now: float, allow_stale: bool) -> list[dict[str, Any]] | None:
    with CACHE_LOCK:
        games = CAL_CACHE.get(comp_id)
        age = now - CAL_CACHE_TIME.get(comp_id, 0)
        if games is not None and (age < CACHE_TTL_SEC or (allow_stale and age < CACHE_MAX_STALE_SEC)):
            return games
    return None


def fetch_mlbl_games(comp_id: int) -> list[dict[str, Any]]:
    """Fetch a division calendar with a lock-protected, bounded-staleness cache."""
    now = time.time()
    fresh = _cached_games(comp_id, now, allow_stale=False)
    if fresh is not None:
        return fresh
    # One request refreshes the shared calendar cache at a time; a second
    # caller rechecks after waiting rather than causing a thundering herd.
    with CACHE_REFRESH_LOCK:
        now = time.time()
        fresh = _cached_games(comp_id, now, allow_stale=False)
        if fresh is not None:
            return fresh
        url = f"https://reg.infobasket.su/Comp/GetCalendar/?comps={comp_id}&format=json"
        try:
            response = requests.get(
                url,
                headers={"User-Agent": "MLBLTelegramBot/1.0", "Referer": "https://moscow.ilovebasket.ru/"},
                timeout=10,
            )
            raw_games = response.json()
        except Exception:
            return _cached_games(comp_id, now, allow_stale=True) or []
        if not isinstance(raw_games, list):
            return _cached_games(comp_id, now, allow_stale=True) or []
        games: list[dict[str, Any]] = []
        for raw_game in raw_games:
            if not isinstance(raw_game, dict):
                continue
            date_value = str(raw_game.get("GameDate", ""))
            time_value = str(raw_game.get("GameTime", "00:00") or "00:00")
            try:
                game_time = datetime.strptime(f"{date_value} {time_value}", "%d.%m.%Y %H:%M")
            except ValueError:
                game_time = datetime.min
            games.append({
                "id": raw_game.get("GameID"),
                "datetime": game_time,
                "date_str": date_value,
                "time_str": time_value,
                "arena": raw_game.get("ArenaRu") or "Зал уточняется",
                "team1": str(raw_game.get("ShortTeamNameAru") or raw_game.get("TeamNameAru") or "").strip(),
                "team2": str(raw_game.get("ShortTeamNameBru") or raw_game.get("TeamNameBru") or "").strip(),
                "status": raw_game.get("GameStatus", 0),
                "score_a": raw_game.get("ScoreA", 0),
                "score_b": raw_game.get("ScoreB", 0),
                "div_name": raw_game.get("LeagueNameRu", ""),
            })
        games.sort(key=lambda game: game["datetime"])
        with CACHE_LOCK:
            CAL_CACHE[comp_id] = games
            CAL_CACHE_TIME[comp_id] = now
        return games


def filter_team_games(games: list[dict[str, Any]], team_name: str) -> list[dict[str, Any]]:
    if not team_name:
        return []
    matched: list[dict[str, Any]] = []
    for game in games:
        is_home = match_text(team_name, game.get("team1", ""))
        is_away = match_text(team_name, game.get("team2", ""))
        if is_home or is_away:
            matched.append({
                **game,
                "is_home": is_home,
                "form": "⚪️ СВЕТЛАЯ" if is_home else "⚫️ ЧЁРНАЯ",
                "opponent": game.get("team2", "") if is_home else game.get("team1", ""),
            })
    return matched


COMPETITIONS = [
    {"id": 142698, "name": "Элита"}, {"id": 142702, "name": "Претенденты (1)"},
    {"id": 142703, "name": "Претенденты (2)"}, {"id": 142704, "name": "Лига Развития (1)"},
    {"id": 142705, "name": "Лига Развития (2)"}, {"id": 142706, "name": "Лига Развития (3)"},
    {"id": 142707, "name": "Лига Развития (4)"}, {"id": 142699, "name": "Lite"},
    {"id": 142708, "name": "Лига развития (4). Дружба"}, {"id": 142717, "name": "W-Элита"},
    {"id": 142718, "name": "W-MLBL (2)"}, {"id": 142711, "name": "Ветераны 35+ (1)"},
    {"id": 142712, "name": "Ветераны 35+ (2)"}, {"id": 142714, "name": "Ветераны 40+ Дружба"},
    {"id": 142715, "name": "Ветераны 45+ Дружба"}, {"id": 142716, "name": "Ветераны 50+"},
    {"id": 142719, "name": "МЛБЛ-Дети U18 (1)"}, {"id": 142720, "name": "МЛБЛ-Дети U18 (2)"},
    {"id": 142721, "name": "МЛБЛ-Дети U16 (1)"}, {"id": 142722, "name": "МЛБЛ-Дети U16 (2)"},
    {"id": 142723, "name": "МЛБЛ-Дети U14 (1)"}, {"id": 142724, "name": "МЛБЛ-Дети U14 (2)"},
    {"id": 142725, "name": "МЛБЛ-Дети U12 (1)"},
]


def auto_find_team(team_name: str, preferred_comp_name: str = "") -> tuple[int, str, str] | None:
    parsed_team, parsed_division = parse_team_and_division(team_name)
    team_name = parsed_team or team_name
    preferred_comp_name = preferred_comp_name or parsed_division
    if not team_name:
        return None
    candidates: list[tuple[int, str, str]] = []
    for competition in COMPETITIONS:
        if preferred_comp_name and not match_text(preferred_comp_name, competition["name"]):
            continue
        matches = filter_team_games(fetch_mlbl_games(competition["id"]), team_name)
        if matches:
            game = matches[0]
            exact = game["team1"] if match_text(team_name, game["team1"]) else game["team2"]
            candidates.append((competition["id"], competition["name"], exact))
    if candidates:
        return candidates[0]
    return auto_find_team(team_name, "") if preferred_comp_name else None


# Telegram's legacy Markdown mode treats these characters as formatting syntax.
def escape_markdown(value: object) -> str:
    return re.sub(r"([\\_*\[\]`])", r"\\\1", str(value if value is not None else ""))


def format_card(game: dict[str, Any], title: str = "Матч", div_name: str = "") -> str:
    division = f"🏆 *Дивизион:* {escape_markdown(div_name)}\n" if div_name else ""
    return (
        f"🏀 *{escape_markdown(title)}*\n\n"
        f"⚔️ *Игра:* {escape_markdown(game.get('team1'))} — {escape_markdown(game.get('team2'))}\n"
        f"{division}"
        f"📅 *Когда:* {escape_markdown(game.get('date_str'))} в {escape_markdown(game.get('time_str'))}\n"
        f"📍 *Зал:* {escape_markdown(game.get('arena'))}\n"
        f"👕 *Форма:* {escape_markdown(game.get('form'))}"
    )


def split_message_chunks(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split outbound text at natural boundaries, always respecting Telegram's limit."""
    if limit < 1:
        raise ValueError("message limit must be positive")
    text = str(text or "")
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        boundary = max(remaining.rfind("\n", 0, limit + 1), remaining.rfind(" ", 0, limit + 1))
        if boundary <= 0:
            boundary = limit
        chunk = remaining[:boundary]
        if chunk.endswith("\\") and boundary < len(remaining):
            chunk = chunk[:-1]
            boundary -= 1
        if not chunk:
            chunk, boundary = remaining[:limit], limit
        chunks.append(chunk)
        remaining = remaining[boundary:]
        if remaining.startswith("\n") or remaining.startswith(" "):
            remaining = remaining[1:]
    chunks.append(remaining)
    return chunks


def _telegram_result(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _is_disabled_error(payload: Mapping[str, Any]) -> bool:
    description = str(payload.get("description") or "").lower()
    return payload.get("error_code") == 403 or any(value in description for value in ("kicked", "deactivated", "chat not found"))


def _send_one_chunk(chat_id: int | str, text: str) -> int | None:
    endpoint = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    for attempt in range(SEND_MAX_ATTEMPTS):
        try:
            response = requests.post(endpoint, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
            payload = _telegram_result(response)
        except Exception as exc:
            if attempt + 1 >= SEND_MAX_ATTEMPTS:
                safe_print(f"Unable to send Telegram message to chat {chat_id}: {exc}")
                return None
            time.sleep(float(2 ** attempt))
            continue
        if payload.get("ok"):
            return payload.get("result", {}).get("message_id")
        if getattr(response, "status_code", None) == 429 or payload.get("error_code") == 429:
            try:
                retry_after = max(0, int(payload.get("parameters", {}).get("retry_after", 5)))
            except (TypeError, ValueError):
                retry_after = 5
            if attempt + 1 < SEND_MAX_ATTEMPTS:
                time.sleep(retry_after + 1.0)
                continue
            return None
        if _is_disabled_error(payload):
            disable_chat(chat_id, str(payload.get("description") or "forbidden"))
            return None
        # A markup parse error should be retried once as plain text.  Dynamic
        # values are escaped above, but this preserves compatibility with old
        # command responses and data from unusual upstream sources.
        description = str(payload.get("description") or "").lower()
        if "parse" in description and "entit" in description:
            try:
                plain_response = requests.post(endpoint, json={"chat_id": chat_id, "text": text}, timeout=10)
                plain_payload = _telegram_result(plain_response)
                if plain_payload.get("ok"):
                    return plain_payload.get("result", {}).get("message_id")
                if _is_disabled_error(plain_payload):
                    disable_chat(chat_id, str(plain_payload.get("description") or "forbidden"))
            except Exception:
                pass
            return None
        if getattr(response, "status_code", 0) >= 500 and attempt + 1 < SEND_MAX_ATTEMPTS:
            time.sleep(float(2 ** attempt))
            continue
        return None
    return None


def send_msg(chat_id: int | str, text: str) -> int | None:
    """Send bounded-size chunks with finite, iterative retries; return last ID."""
    last_message_id: int | None = None
    for chunk in split_message_chunks(text):
        message_id = _send_one_chunk(chat_id, chunk)
        if message_id is None:
            return None
        last_message_id = message_id
    return last_message_id


def delete_msg(chat_id: int | str, message_id: int) -> None:
    try:
        result = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/deleteMessage",
            json={"chat_id": chat_id, "message_id": int(message_id)}, timeout=5,
        ).json()
        if not result.get("ok"):
            safe_print(f"Could not delete Telegram message {message_id}.")
    except Exception:
        safe_print(f"Unable to delete Telegram message {message_id}.")


def delete_later(chat_id: int | str, message_ids: list[Any], delay: float = 3.0) -> None:
    def worker() -> None:
        if delay > 0:
            time.sleep(delay)
        valid_ids: list[int] = []
        for message_id in message_ids:
            if message_id:
                try:
                    valid_ids.append(int(message_id))
                except (TypeError, ValueError):
                    pass
        valid_ids = sorted(set(valid_ids))
        if not valid_ids:
            return
        try:
            result = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/deleteMessages",
                json={"chat_id": chat_id, "message_ids": valid_ids}, timeout=5,
            ).json()
            if result.get("ok"):
                return
        except Exception:
            pass
        for index, message_id in enumerate(valid_ids):
            delete_msg(chat_id, message_id)
            if index < len(valid_ids) - 1:
                time.sleep(0.3)
    threading.Thread(target=worker, daemon=True).start()


def _sleep_until_stop(stop_event: threading.Event, seconds: float) -> bool:
    """Wait responsively for shutdown while retaining testable sleep semantics."""
    deadline = time.monotonic() + max(0.0, seconds)
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(1.0, remaining))
    return True


def reminder_loop(stop_event: threading.Event | None = None) -> None:
    """Send idempotent 24-hour and 3-hour reminders for configured chats."""
    stop_event = stop_event or STOP_EVENT
    time.sleep(5)  # Retains the original startup grace period.
    while not stop_event.is_set():
        try:
            now = get_moscow_now()
            config = load_chats_config()
            changed = False
            for chat_id, info in config.items():
                if not isinstance(info, dict) or info.get("enabled") is False:
                    continue
                team, comp_id = info.get("team"), info.get("comp_id")
                if not team or not comp_id:
                    continue
                stages = info.setdefault("reminded_stages", [])
                if not isinstance(stages, list):
                    stages = info["reminded_stages"] = []
                    changed = True
                for old_id in info.pop("reminded_ids", []):
                    stage = f"{old_id}:24h"
                    if stage not in stages:
                        stages.append(stage)
                        changed = True
                for game in filter_team_games(fetch_mlbl_games(comp_id), team):
                    if game.get("status") != 0 or not game.get("id") or not game.get("datetime"):
                        continue
                    hours = (game["datetime"] - now).total_seconds() / 3600.0
                    stage_24 = f"{game['id']}:24h"
                    if 18.0 <= hours <= 30.0 and stage_24 not in stages:
                        if send_msg(chat_id, f"🔔 *Внимание! Завтра игра:*\n\n{format_card(game, 'Завтра матч', info.get('division', ''))}"):
                            stages.append(stage_24)
                            changed = True
                    stage_3 = f"{game['id']}:3h"
                    if 0.5 <= hours <= 3.5 and stage_3 not in stages:
                        if send_msg(chat_id, f"🔥 *Внимание! До игры осталось 3 часа:*\n\n{format_card(game, 'Матч через 3 часа', info.get('division', ''))}"):
                            stages.append(stage_3)
                            changed = True
            if changed:
                def persist_reminder_changes(current: dict[str, Any]) -> None:
                    for chat_id, updated_info in config.items():
                        if chat_id not in current or not isinstance(updated_info, dict):
                            continue
                        current_info = current.get(chat_id)
                        if not isinstance(current_info, dict):
                            continue
                        current_info["reminded_stages"] = list(updated_info.get("reminded_stages", []))
                        current_info.pop("reminded_ids", None)
                update_chats_config(persist_reminder_changes)
        except Exception as exc:
            safe_print(f"Reminder loop error: {exc}")
        # Wait responsively for shutdown.  Calling time.sleep retains backwards
        # compatibility with embedders/tests that patch it to break the loop.
        if _sleep_until_stop(stop_event, CHECK_INTERVAL_SEC):
            break


def handle_game_query(chat_id: str, query: str = "") -> None:
    config = load_chats_config().get(chat_id)
    now = get_moscow_now()
    if query:
        is_date = bool(re.search(r"^\d{1,2}[\./]\d{1,2}", query.strip()))
        if is_date:
            if not config or not config.get("team"):
                send_msg(chat_id, "⚠️ Этот чат еще не привязан к команде.\nАдминистратор должен отправить: `/set НазваниеКоманды`")
                return
            team, comp_id = config["team"], config.get("comp_id")
            games = filter_team_games(fetch_mlbl_games(comp_id), team) if comp_id else []
            parts = query.strip().replace("/", ".").split(".")
            norm_date = f"{parts[0].zfill(2)}.{parts[1].zfill(2)}" if len(parts) > 1 else parts[0].zfill(2)
            matches = [game for game in games if str(game.get("date_str", "")).startswith(norm_date)]
            if matches:
                for game in matches:
                    label = "Запланированный матч" if game.get("status") == 0 else f"Счёт: {game.get('score_a')}:{game.get('score_b')}"
                    send_msg(chat_id, format_card(game, f"{label} на {game.get('date_str')}", config.get("division", "")))
            else:
                send_msg(chat_id, f"На дату `{escape_markdown(query.strip())}` игр для команды *{escape_markdown(team)}* не найдено.")
            return
        team_in, division_in = parse_team_and_division(query)
        if not team_in:
            send_msg(chat_id, "Укажите команду или дату:\n`/game НазваниеКоманды`")
            return
        found = auto_find_team(team_in, division_in)
        if found:
            comp_id, division, exact_team = found
            games = filter_team_games(fetch_mlbl_games(comp_id), exact_team)
            upcoming = [game for game in games if game.get("status") == 0 and game.get("datetime") >= now]
            future = upcoming or [game for game in games if game.get("status") == 0]
            if future:
                send_msg(chat_id, format_card(future[0], "Ближайшая игра", division))
            else:
                send_msg(chat_id, f"Все запланированные матчи для команды *{escape_markdown(exact_team)}* ({escape_markdown(division)}) уже завершены.")
            return
        if config and config.get("team") and config.get("comp_id"):
            games = filter_team_games(fetch_mlbl_games(config["comp_id"]), config["team"])
            matches = [game for game in games if team_in.lower() in str(game.get("date_str", "")).lower() or team_in.lower() in str(game.get("arena", "")).lower()]
            if matches:
                for game in matches:
                    label = "Запланированный матч" if game.get("status") == 0 else f"Счёт: {game.get('score_a')}:{game.get('score_b')}"
                    send_msg(chat_id, format_card(game, label, config.get("division", "")))
                return
        send_msg(chat_id, f"❌ Команда или матч `{escape_markdown(query)}` не найдены в расписании МЛБЛ.")
        return
    if not config or not config.get("team"):
        send_msg(chat_id, "⚠️ Этот чат еще не привязан к команде.\nАдминистратор должен отправить: `/set НазваниеКоманды`")
        return
    team, comp_id = config["team"], config.get("comp_id")
    if not comp_id:
        send_msg(chat_id, "⚠️ Ошибка конфигурации команды. Попробуйте привязать заново через `/set`.")
        return
    games = filter_team_games(fetch_mlbl_games(comp_id), team)
    upcoming = [game for game in games if game.get("status") == 0 and game.get("datetime") >= now]
    future = upcoming or [game for game in games if game.get("status") == 0]
    if future:
        send_msg(chat_id, format_card(future[0], "Ближайшая игра", config.get("division", "")))
    else:
        send_msg(chat_id, f"Все запланированные матчи для команды *{escape_markdown(team)}* уже завершены.")


def handle_schedule_query(chat_id: str, query: str = "") -> None:
    config = load_chats_config().get(chat_id)
    if query:
        team_in, division_in = parse_team_and_division(query)
        if not team_in:
            send_msg(chat_id, "Укажите название команды:\n`/schedule НазваниеКоманды`")
            return
        found = auto_find_team(team_in, division_in)
        if not found:
            send_msg(chat_id, f"❌ Команда `{escape_markdown(query)}` не найдена в расписании МЛБЛ.")
            return
        comp_id, division, team = found
    elif config and config.get("team"):
        team, comp_id, division = config["team"], config.get("comp_id"), config.get("division", "")
    else:
        send_msg(chat_id, "⚠️ Чат еще не привязан к команде. Отправьте `/set НазваниеКоманды` или `/schedule НазваниеКоманды`.")
        return
    if not comp_id:
        send_msg(chat_id, "⚠️ Ошибка конфигурации команды. Попробуйте привязать заново через `/set`.")
        return
    games = filter_team_games(fetch_mlbl_games(comp_id), team)
    if not games:
        send_msg(chat_id, f"Расписание для *{escape_markdown(team)}* пока не найдено.")
        return
    upcoming = [game for game in games if game.get("status") == 0]
    finished = [game for game in games if game.get("status") == 1]
    lines = [f"📅 *Календарь матчей {escape_markdown(team)} ({escape_markdown(division)}):*\n"]
    if upcoming:
        lines.append("*⏳ Запланированные матчи:*")
        for game in upcoming:
            lines.append(
                f"• *{escape_markdown(game.get('date_str'))}* в *{escape_markdown(game.get('time_str'))}* — "
                f"vs {escape_markdown(game.get('opponent'))} ({escape_markdown(game.get('form'))}) | {escape_markdown(game.get('arena'))}"
            )
    if finished:
        lines.append("\n*🏁 Завершённые матчи:*")
        for game in finished:
            lines.append(
                f"• {escape_markdown(game.get('date_str'))} — {escape_markdown(game.get('team1'))} "
                f"{escape_markdown(game.get('score_a'))}:{escape_markdown(game.get('score_b'))} {escape_markdown(game.get('team2'))}"
            )
    send_msg(chat_id, "\n".join(lines))


def process_message(message: dict[str, Any]) -> None:
    try:
        if not isinstance(message, dict) or not isinstance(message.get("text"), str):
            return
        raw_text = message["text"].strip()
        if not raw_text.startswith("/"):
            return
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            return
        user_id = (message.get("from") or {}).get("id", 0)
        chat_type = chat.get("type", "")
        is_private, is_channel = chat_type == "private", chat_type == "channel"
        sender_chat_id = (message.get("sender_chat") or {}).get("id")
        is_anonymous_admin = bool(sender_chat_id and str(sender_chat_id) == chat_id)
        parts = raw_text.split(None, 1)
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""
        config = load_chats_config()
        if command in ("/start", "/help"):
            if is_private:
                send_msg(chat_id, "👋 Привет! Я бот расписания лиги МЛБЛ Москва.\n\n🏀 *Как подключить:*\n1. Добавьте меня в чат вашей команды.\n2. Администратор чата должен отправить:\n`/set НазваниеКоманды`\n\nБот сам найдет дивизион команды и будет напоминать о матчах накануне!")
            elif chat_id in config and config[chat_id].get("team"):
                info = config[chat_id]
                send_msg(chat_id, f"🏀 Чат привязан к: *{escape_markdown(info['team'])}* ({escape_markdown(info.get('division', ''))})\n\n• `/game` — ближайший матч и цвет формы\n• `/game 11.10` — матч на дату\n• `/schedule` — календарь матчей на сезон\n• Напоминания приходят автоматически за 1 день до игры.")
            else:
                send_msg(chat_id, "👋 Чтобы настроить бота, администратор должен отправить:\n`/set НазваниеКоманды`")
            return
        if command in ("/set", "/team"):
            if not is_private and not is_channel and not is_anonymous_admin and not is_group_admin(chat_id, user_id):
                warning_id = send_msg(chat_id, "⛔️ Только администратор этой группы может настраивать команду.")
                delete_later(chat_id, [warning_id, message.get("message_id")], delay=2.5)
                return
            if not args:
                send_msg(chat_id, "Укажите команду:\n`/set НазваниеКоманды`")
                return
            team_in, division_in = parse_team_and_division(args)
            if not team_in:
                send_msg(chat_id, "Укажите команду:\n`/set НазваниеКоманды`")
                return
            found = auto_find_team(team_in, division_in)
            if not found:
                send_msg(chat_id, f"❌ Команда `{escape_markdown(team_in)}` не найдена в расписании МЛБЛ. Проверьте название на moscow.ilovebasket.ru.")
                return
            comp_id, division, exact_team = found
            def bind_team(current: dict[str, Any]) -> None:
                current[chat_id] = {
                    "team": exact_team,
                    "comp_id": comp_id,
                    "division": division,
                    "reminded_stages": [],
                    "enabled": True,
                }
            update_chats_config(bind_team)
            send_msg(chat_id, f"✅ Чат успешно привязан!\n\n🏀 Команда: *{escape_markdown(exact_team)}*\n🏆 Дивизион: *{escape_markdown(division)}*\n\n• За 1 день до каждого матча придёт автоматическое напоминание.\n• Напишите `/game` (или `/game ДД.ММ`) и `/schedule`, чтобы посмотреть календарь.")
            return
        if command in ("/unset", "/reset"):
            if not is_private and not is_channel and not is_anonymous_admin and not is_group_admin(chat_id, user_id):
                warning_id = send_msg(chat_id, "⛔️ Только администратор этой группы может отвязать команду.")
                delete_later(chat_id, [warning_id, message.get("message_id")], delay=2.5)
                return
            if chat_id in config:
                old_team = config[chat_id].get("team", "")
                def unbind_team(current: dict[str, Any]) -> None:
                    current.pop(chat_id, None)
                update_chats_config(unbind_team)
                send_msg(chat_id, f"🗑 Привязка к команде *{escape_markdown(old_team)}* удалена.")
            else:
                send_msg(chat_id, "ℹ️ К этому чату не привязана ни одна команда.")
            return
        if command in ("/game", "/mlbl"):
            handle_game_query(chat_id, args)
        elif command in ("/schedule", "/calendar"):
            handle_schedule_query(chat_id, args)
        elif command in ("/abl", "/next"):
            send_msg(chat_id, "🏀 Этот бот предназначен для лиги МЛБЛ Москва.\nИспользуйте команду `/game` для просмотра расписания матчей МЛБЛ.")
    except Exception as exc:
        safe_print(f"Message processing error: {exc}")


def health_status() -> tuple[int, dict[str, str]]:
    return 200, {"status": "ok", "service": "mlbl-telegram-bot"}


def readiness_status() -> tuple[int, dict[str, str]]:
    if not TOKEN:
        return 503, {"status": "not_ready", "reason": "BOT_TOKEN is not configured"}
    try:
        _path(CHATS_FILE).parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 503, {"status": "not_ready", "reason": "data directory is unavailable"}
    if STOP_EVENT.is_set():
        return 503, {"status": "not_ready", "reason": "service is stopping"}
    if not SERVICE_STARTED.is_set():
        return 503, {"status": "not_ready", "reason": "Telegram polling has not started"}
    if LAST_POLL_SUCCESS_AT and time.monotonic() - LAST_POLL_SUCCESS_AT > HEALTH_STALE_AFTER_SEC:
        return 503, {"status": "not_ready", "reason": "Telegram polling is stale"}
    return 200, {"status": "ready"}


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (HTTP method name)
        if self.path.split("?", 1)[0] == "/healthz":
            status, body = health_status()
        elif self.path.split("?", 1)[0] == "/readyz":
            status, body = readiness_status()
        else:
            status, body = 404, {"status": "not_found"}
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_render_web_server(stop_event: threading.Event | None = None, port: int | None = None) -> None:
    """Serve liveness/readiness endpoints until a supplied stop event is set."""
    stop_event = stop_event or STOP_EVENT
    try:
        with _ReusableThreadingTCPServer(("", port or SETTINGS.port), _StatusHandler) as server:
            server.timeout = 0.5
            while not stop_event.is_set():
                server.handle_request()
    except OSError as exc:
        safe_print(f"Health server could not start: {exc}")


def _install_stop_handlers(stop_event: threading.Event) -> None:
    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()
    try:
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
    except (ValueError, AttributeError):  # non-main thread/platform
        pass


def run_bot(stop_event: threading.Event | None = None) -> None:
    """Long-poll Telegram with a persisted offset and cooperative shutdown."""
    global LAST_POLL_SUCCESS_AT
    stop_event = stop_event or STOP_EVENT
    if not TOKEN:
        safe_print("BOT_TOKEN is not configured; bot polling was not started.")
        return
    _install_stop_handlers(stop_event)
    SERVICE_STARTED.set()
    threading.Thread(target=start_render_web_server, args=(stop_event,), daemon=True).start()
    threading.Thread(target=reminder_loop, args=(stop_event,), daemon=True).start()
    # Do not discard updates on start.  Operators may explicitly opt in via
    # DROP_PENDING_UPDATES=true for an intentional reset.
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/deleteWebhook",
            json={"drop_pending_updates": SETTINGS.drop_pending_updates}, timeout=10,
        )
    except Exception:
        pass
    offset = load_update_offset()
    errors = 0
    while not stop_event.is_set():
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 25}, timeout=30,
            )
            payload = _telegram_result(response)
            if getattr(response, "status_code", None) == 429 or payload.get("error_code") == 429:
                try:
                    delay = max(0, int(payload.get("parameters", {}).get("retry_after", 5))) + 1.0
                except (TypeError, ValueError):
                    delay = 6.0
                stop_event.wait(delay)
                continue
            if not payload.get("ok"):
                errors += 1
                stop_event.wait(min(1.5 * (2 ** (errors - 1)), 30.0))
                continue
            errors = 0
            LAST_POLL_SUCCESS_AT = time.monotonic()
            for update in payload.get("result", []):
                if stop_event.is_set() or not isinstance(update, dict):
                    break
                update_id = update.get("update_id")
                message = update.get("message") or update.get("channel_post")
                if message:
                    process_message(message)
                try:
                    next_offset = int(update_id) + 1
                except (TypeError, ValueError):
                    continue
                if next_offset > offset:
                    offset = next_offset
                    # Persist only after processing: a crash can duplicate an
                    # update, but cannot silently lose one.
                    save_update_offset(offset)
        except Exception as exc:
            errors += 1
            safe_print(f"Telegram polling error: {exc}")
            stop_event.wait(min(1.5 * (2 ** (errors - 1)), 30.0))
    SERVICE_STARTED.clear()


if __name__ == "__main__":
    run_bot()
