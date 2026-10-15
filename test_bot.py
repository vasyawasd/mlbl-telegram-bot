import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
import main

class TestMlblBot(unittest.TestCase):
    def setUp(self):
        self.mock_config = {}
        def _save(cfg):
            self.mock_config.clear()
            self.mock_config.update(cfg)
        self.patcher_load = patch("main.load_chats_config", side_effect=lambda: dict(self.mock_config))
        self.patcher_save = patch("main.save_chats_config", side_effect=_save)
        self.mock_load = self.patcher_load.start()
        self.mock_save = self.patcher_save.start()
        main.CAL_CACHE.clear()
        main.CAL_CACHE_TIME.clear()

    def tearDown(self):
        self.patcher_load.stop()
        self.patcher_save.stop()

    # --- 1. Fetching games, filtering & formatting ---

    @patch("main.requests.get")
    def test_fetch_mlbl_games_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "GameID": 1001,
                "GameDate": "18.10.2026",
                "GameTime": "15:00",
                "ArenaRu": "Арена Медина",
                "ShortTeamNameAru": "Спартак",
                "ShortTeamNameBru": "ЦСКА",
                "GameStatus": 0,
                "ScoreA": 0,
                "ScoreB": 0,
                "LeagueNameRu": "Элита"
            }
        ]
        mock_get.return_value = mock_resp

        games = main.fetch_mlbl_games(142698)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["id"], 1001)
        self.assertEqual(games[0]["team1"], "Спартак")
        self.assertEqual(games[0]["team2"], "ЦСКА")
        self.assertEqual(games[0]["datetime"], datetime(2026, 10, 18, 15, 0))

    @patch("main.requests.get")
    def test_fetch_mlbl_games_error_handling(self, mock_get):
        mock_get.side_effect = Exception("API down")
        games = main.fetch_mlbl_games(142698)
        self.assertEqual(games, [])

    def test_filter_team_games(self):
        games = [
            {
                "id": 1,
                "team1": "БК Путилково",
                "team2": "Метеор",
                "date_str": "18.10.2026",
                "time_str": "12:00",
                "status": 0
            },
            {
                "id": 2,
                "team1": "Глория",
                "team2": "БК Путилково",
                "date_str": "25.10.2026",
                "time_str": "14:00",
                "status": 0
            }
        ]
        res = main.filter_team_games(games, "Путилково")
        self.assertEqual(len(res), 2)
        # Игра 1: команда слева (хозяева) -> белая форма
        self.assertTrue(res[0]["is_home"])
        self.assertEqual(res[0]["form"], "⚪️ СВЕТЛАЯ")
        self.assertEqual(res[0]["opponent"], "Метеор")
        # Игра 2: команда справа (гости) -> темная форма
        self.assertFalse(res[1]["is_home"])
        self.assertEqual(res[1]["form"], "⚫️ ЧЁРНАЯ")
        self.assertEqual(res[1]["opponent"], "Глория")

    def test_format_card(self):
        game = {
            "team1": "Спартак",
            "team2": "Динамо",
            "date_str": "18.10.2026",
            "time_str": "19:00",
            "arena": "Дворец Спорта",
            "form": "⚪️ СВЕТЛАЯ"
        }
        card = main.format_card(game, title="Ближайшая игра", div_name="Элита")
        self.assertIn("Спартак — Динамо", card)
        self.assertIn("18.10.2026 в 19:00", card)
        self.assertIn("⚪️ СВЕТЛАЯ", card)
        self.assertIn("🏆 *Дивизион:* Элита", card)

    def test_parse_team_and_division(self):
        self.assertEqual(main.parse_team_and_division("Спартак | Элита"), ("Спартак", "Элита"))
        self.assertEqual(main.parse_team_and_division("Спартак (Элита)"), ("Спартак", "Элита"))
        self.assertEqual(main.parse_team_and_division("Спартак [Элита]"), ("Спартак", "Элита"))
        self.assertEqual(main.parse_team_and_division("БК Путилково"), ("БК Путилково", ""))
        self.assertEqual(main.parse_team_and_division(""), ("", ""))

    def test_match_text_comprehensive(self):
        self.assertTrue(main.match_text("HD X Север", "HDXСЕВЕР"))
        self.assertTrue(main.match_text("HD \u0445 Север", "HDXСЕВЕР"))
        self.assertTrue(main.match_text("hd x sever", "HDxSEVER WOMEN"))
        self.assertTrue(main.match_text("БК Путилково", "БК Путилково"))
        self.assertTrue(main.match_text("Путилково", "БК Путилково"))
        self.assertTrue(main.match_text("БК Путилково", "Путилково"))
        self.assertTrue(main.match_text("Спартак-2", "Спартак 2"))
        self.assertFalse(main.match_text("ЦСКА", "Спартак"))
        self.assertFalse(main.match_text("а", "Спартак"))

    # --- 2. Command handling ---

    @patch("main.send_msg")
    def test_command_start_help_private(self, mock_send):
        msg = {
            "text": "/start",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        mock_send.assert_called_once()
        self.assertIn("Привет!", mock_send.call_args[0][1])

    @patch("main.send_msg")
    def test_command_game_unconfigured_chat_without_args(self, mock_send):
        msg = {
            "text": "/game",
            "chat": {"id": -100111, "type": "group"},
            "from": {"id": 123}
        }
        main.process_message(msg)
        mock_send.assert_called_with("-100111", "⚠️ Этот чат еще не привязан к команде.\nАдминистратор должен отправить: `/set НазваниеКоманды`")

    @patch("main.send_msg")
    @patch("main.fetch_mlbl_games")
    def test_command_game_bound_chat(self, mock_fetch, mock_send):
        self.mock_config["-100111"] = {
            "team": "Спартак",
            "comp_id": 142698,
            "division": "Элита"
        }
        mock_fetch.return_value = [
            {
                "id": 1,
                "team1": "Спартак",
                "team2": "ЦСКА",
                "date_str": "20.10.2026",
                "time_str": "19:00",
                "arena": "Медина",
                "status": 0,
                "datetime": datetime(2026, 10, 20, 19, 0),
                "score_a": 0,
                "score_b": 0,
                "div_name": "Элита"
            }
        ]
        msg = {
            "text": "/game",
            "chat": {"id": -100111, "type": "group"},
            "from": {"id": 123}
        }
        main.process_message(msg)
        mock_send.assert_called_once()
        self.assertIn("Спартак — ЦСКА", mock_send.call_args[0][1])

    @patch("main.send_msg")
    @patch("main.auto_find_team")
    @patch("main.fetch_mlbl_games")
    def test_command_game_specific_team(self, mock_fetch, mock_find, mock_send):
        mock_find.return_value = (142698, "Элита", "ЦСКА")
        mock_fetch.return_value = [
            {
                "id": 2,
                "team1": "ЦСКА",
                "team2": "Зенит",
                "date_str": "22.10.2026",
                "time_str": "20:00",
                "arena": "ЦСКА Арена",
                "status": 0,
                "datetime": datetime(2026, 10, 22, 20, 0),
                "score_a": 0,
                "score_b": 0,
                "div_name": "Элита"
            }
        ]
        msg = {
            "text": "/game ЦСКА",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        mock_find.assert_called_with("ЦСКА", "")
        mock_send.assert_called_once()
        self.assertIn("ЦСКА — Зенит", mock_send.call_args[0][1])

    @patch("main.send_msg")
    @patch("main.fetch_mlbl_games")
    def test_command_game_date_filter(self, mock_fetch, mock_send):
        self.mock_config["12345"] = {
            "team": "Спартак",
            "comp_id": 142698,
            "division": "Элита"
        }
        mock_fetch.return_value = [
            {
                "id": 1,
                "team1": "Спартак",
                "team2": "ЦСКА",
                "date_str": "18.10.2026",
                "time_str": "19:00",
                "arena": "Медина",
                "status": 0,
                "datetime": datetime(2026, 10, 18, 19, 0),
                "score_a": 0,
                "score_b": 0,
                "div_name": "Элита"
            }
        ]
        msg = {
            "text": "/game 18.10",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        mock_send.assert_called_once()
        self.assertIn("18.10.2026", mock_send.call_args[0][1])

    @patch("main.send_msg")
    @patch("main.fetch_mlbl_games")
    def test_command_game_bot_mention_with_date(self, mock_fetch, mock_send):
        self.mock_config["-100222"] = {
            "team": "Спартак",
            "comp_id": 142698,
            "division": "Элита"
        }
        mock_fetch.return_value = [
            {
                "id": 1,
                "team1": "Спартак",
                "team2": "ЦСКА",
                "date_str": "18.10.2026",
                "time_str": "19:00",
                "arena": "Медина",
                "status": 0,
                "datetime": datetime(2026, 10, 18, 19, 0),
                "score_a": 0,
                "score_b": 0,
                "div_name": "Элита"
            }
        ]
        msg = {
            "text": "/game@mlbl_basket_bot 18.10",
            "chat": {"id": -100222, "type": "group"},
            "from": {"id": 123}
        }
        main.process_message(msg)
        mock_send.assert_called_once()
        self.assertIn("18.10.2026", mock_send.call_args[0][1])

    @patch("main.send_msg")
    def test_command_abl_cross_info(self, mock_send):
        msg = {
            "text": "/abl",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        mock_send.assert_called_once()
        self.assertIn("Этот бот предназначен для лиги МЛБЛ Москва", mock_send.call_args[0][1])

    @patch("main.send_msg")
    @patch("main.fetch_mlbl_games")
    def test_command_schedule(self, mock_fetch, mock_send):
        self.mock_config["12345"] = {
            "team": "Спартак",
            "comp_id": 142698,
            "division": "Элита"
        }
        mock_fetch.return_value = [
            {
                "id": 1,
                "team1": "Спартак",
                "team2": "ЦСКА",
                "date_str": "18.10.2026",
                "time_str": "19:00",
                "arena": "Медина",
                "status": 0,
                "datetime": datetime(2026, 10, 18, 19, 0),
                "score_a": 0,
                "score_b": 0,
                "div_name": "Элита"
            }
        ]
        msg = {
            "text": "/schedule",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        mock_send.assert_called_once()
        self.assertIn("Календарь матчей Спартак", mock_send.call_args[0][1])

    # --- 3. Non-slash message filtering ---

    @patch("main.send_msg")
    def test_ignore_plain_text(self, mock_send):
        for text in ["игра", "форма", "завтра играем?", "привет", "18.10"]:
            msg = {
                "text": text,
                "chat": {"id": -100222, "type": "group"},
                "from": {"id": 123}
            }
            main.process_message(msg)
        mock_send.assert_not_called()

    @patch("main.send_msg")
    def test_ignore_non_text(self, mock_send):
        msg = {
            "sticker": {"file_id": "stk123"},
            "chat": {"id": -100222, "type": "group"},
            "from": {"id": 123}
        }
        main.process_message(msg)
        mock_send.assert_not_called()

    # --- 4. Admin checks & message deletion logic ---

    @patch("main.send_msg")
    @patch("main.delete_later")
    @patch("main.is_group_admin", return_value=False)
    def test_set_rejected_non_admin(self, mock_admin, mock_delete, mock_send):
        mock_send.return_value = 888
        msg = {
            "message_id": 777,
            "text": "/set Спартак",
            "chat": {"id": -100333, "type": "group"},
            "from": {"id": 555}
        }
        main.process_message(msg)
        mock_send.assert_called_with("-100333", "⛔️ Только администратор этой группы может настраивать команду.")
        mock_delete.assert_called_once_with("-100333", [888, 777], delay=2.5)
        self.assertNotIn("-100333", self.mock_config)

    @patch("main.send_msg")
    @patch("main.auto_find_team")
    @patch("main.is_group_admin", return_value=True)
    def test_set_allowed_admin(self, mock_admin, mock_find, mock_send):
        mock_find.return_value = (142698, "Элита", "Спартак")
        msg = {
            "message_id": 777,
            "text": "/set Спартак",
            "chat": {"id": -100333, "type": "group"},
            "from": {"id": 555}
        }
        main.process_message(msg)
        self.assertIn("-100333", self.mock_config)
        self.assertEqual(self.mock_config["-100333"]["team"], "Спартак")
        self.assertEqual(self.mock_config["-100333"]["comp_id"], 142698)

    @patch("main.send_msg")
    @patch("main.is_group_admin", return_value=True)
    def test_unset_command(self, mock_admin, mock_send):
        self.mock_config["-100333"] = {"team": "Спартак", "comp_id": 142698, "division": "Элита"}
        msg = {
            "text": "/unset",
            "chat": {"id": -100333, "type": "group"},
            "from": {"id": 555}
        }
        main.process_message(msg)
        self.assertNotIn("-100333", self.mock_config)
        self.assertIn("удалена", mock_send.call_args[0][1])

    # --- 5. Message deletion logic tests (delete_msg & delete_later) ---

    @patch("main.requests.post")
    def test_delete_msg_success_and_failure(self, mock_post):
        mock_resp_ok = MagicMock()
        mock_resp_ok.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp_ok

        main.delete_msg("12345", 555)
        mock_post.assert_called_with(
            f"https://api.telegram.org/bot{main.TOKEN}/deleteMessage",
            json={"chat_id": "12345", "message_id": 555},
            timeout=5
        )

        mock_post.side_effect = Exception("Network error")
        try:
            main.delete_msg("12345", 555)
        except Exception:
            self.fail("delete_msg threw an uncaught exception on network error")

    @patch("main.delete_msg")
    @patch("main.requests.post")
    def test_delete_later_batch_success(self, mock_post, mock_del_msg):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        with patch("main.threading.Thread") as mock_thread:
            main.delete_later("12345", [10, 20, "30", None], delay=0)
            mock_thread.assert_called_once()
            worker = mock_thread.call_args[1]["target"]
            worker()

        mock_post.assert_called_once_with(
            f"https://api.telegram.org/bot{main.TOKEN}/deleteMessages",
            json={"chat_id": "12345", "message_ids": [10, 20, 30]},
            timeout=5
        )
        mock_del_msg.assert_not_called()

    @patch("main.delete_msg")
    @patch("main.requests.post")
    def test_delete_later_fallback_to_individual(self, mock_post, mock_del_msg):
        mock_resp_fail = MagicMock()
        mock_resp_fail.json.return_value = {"ok": False, "description": "Not allowed"}
        mock_post.return_value = mock_resp_fail

        with patch("main.threading.Thread") as mock_thread:
            with patch("time.sleep"):
                main.delete_later("12345", [10, 20], delay=0)
                worker = mock_thread.call_args[1]["target"]
                worker()

        self.assertEqual(mock_del_msg.call_count, 2)
        mock_del_msg.assert_any_call("12345", 10)
        mock_del_msg.assert_any_call("12345", 20)

    # --- 6. Edge cases ---

    @patch("main.send_msg")
    def test_sender_chat_none_does_not_crash(self, mock_send):
        msg = {
            "message_id": 100,
            "text": "/game",
            "chat": {"id": -100555, "type": "supergroup"},
            "sender_chat": None,
            "from": {"id": 123}
        }
        main.process_message(msg)
        mock_send.assert_called_once()

    @patch("main.send_msg")
    @patch("main.auto_find_team")
    def test_anonymous_admin_allowed_in_group(self, mock_find, mock_send):
        mock_find.return_value = (142698, "Элита", "Рубин")
        msg = {
            "message_id": 101,
            "text": "/set Рубин",
            "chat": {"id": -100555, "type": "supergroup"},
            "sender_chat": {"id": -100555},
            "from": None
        }
        main.process_message(msg)
        self.assertIn("-100555", self.mock_config)
        self.assertEqual(self.mock_config["-100555"]["team"], "Рубин")

    @patch("main.send_msg")
    @patch("main.auto_find_team")
    def test_channel_post_allowed(self, mock_find, mock_send):
        mock_find.return_value = (142698, "Элита", "Динамо")
        msg = {
            "message_id": 102,
            "text": "/set Динамо",
            "chat": {"id": -100777, "type": "channel"},
            "from": None
        }
        main.process_message(msg)
        self.assertIn("-100777", self.mock_config)
        self.assertEqual(self.mock_config["-100777"]["team"], "Динамо")

    @patch("main.send_msg")
    def test_just_slash_and_unknown_command(self, mock_send):
        for raw in ["/", "/   ", "/unknown_command", "/xyz 123"]:
            msg = {
                "text": raw,
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 12345}
            }
            main.process_message(msg)
        mock_send.assert_not_called()

    @patch("main.send_msg")
    def test_set_without_arguments(self, mock_send):
        msg = {
            "text": "/set",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345}
        }
        main.process_message(msg)
        self.assertIn("Укажите команду", mock_send.call_args[0][1])

    @patch("main.requests.post")
    def test_send_msg_markdown_fallback(self, mock_post):
        mock_resp_fail = MagicMock()
        mock_resp_fail.json.return_value = {"ok": False, "description": "Bad Request: can't parse entities"}
        mock_resp_ok = MagicMock()
        mock_resp_ok.json.return_value = {"ok": True, "result": {"message_id": 8888}}
        mock_post.side_effect = [mock_resp_fail, mock_resp_ok]

        mid = main.send_msg("12345", "Текст с *незакрытой звездочкой")
        self.assertEqual(mid, 8888)
        self.assertEqual(mock_post.call_count, 2)
        self.assertNotIn("parse_mode", mock_post.call_args_list[1][1]["json"])

if __name__ == "__main__":
    unittest.main()

