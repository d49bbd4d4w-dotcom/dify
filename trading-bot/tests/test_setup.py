import sys
import tomllib

import pytest

from tradebot import cli, thesis
from tradebot.config import Config, load_config, save_settings


class FakeTelegram:
    def __init__(self, good_token="123:abc", updates=None):
        self.good_token = good_token
        self.updates = updates if updates is not None else [
            {"channel_post": {"chat": {"id": -1001234, "title": "My Picks", "type": "channel"}}}]
        self.sent = []

    def get_json(self, url, params=None):
        if url.endswith("/getMe"):
            if f"/bot{self.good_token}/" not in url:
                raise RuntimeError("401 Unauthorized")
            return {"ok": True, "result": {"username": "my_picks_bot"}}
        if url.endswith("/getUpdates"):
            return {"ok": True, "result": self.updates}
        if url.endswith("/v4/sports"):
            if params["apiKey"] != "ODDSKEY":
                raise RuntimeError("401")
            return [{"key": "basketball_nba", "active": True}, {"key": "soccer_epl", "active": True}]
        raise AssertionError(url)

    def post_json(self, url, payload):
        self.sent.append((url, payload))


def scripted(*answers):
    it = iter(answers)
    return lambda prompt="": next(it)


def test_setup_writes_working_config(tmp_path, capsys):
    path = tmp_path / "config.toml"
    http = FakeTelegram()
    answers = scripted("$150", "wrong-token", "123:abc", "", "1", "badkey", "ODDSKEY")
    cli.cmd_setup(Config(), http, str(path), ask=answers)
    cfg = load_config(path)
    assert cfg.bankroll == 150 and cfg.telegram_bot_token == "123:abc"
    assert cfg.telegram_chat_id == "-1001234" and cfg.odds_api_key == "ODDSKEY"
    assert http.sent and http.sent[0][1]["chat_id"] == "-1001234"
    out = capsys.readouterr().out
    assert "@my_picks_bot" in out and "didn't work" in out and "Sent a test message" in out
    # Started from the example file, so all the explanations are still in it.
    assert "# Alerts." in path.read_text() and "sports_style" in path.read_text()


def test_setup_waits_for_a_channel_post_and_can_skip_everything(tmp_path):
    path = tmp_path / "config.toml"
    http = FakeTelegram(updates=[])
    # No messages yet -> check again -> still none -> skip Telegram chat; skip odds key.
    cli.cmd_setup(Config(), http, str(path), ask=scripted("", "123:abc", "", "", "skip", ""))
    cfg = load_config(path)
    assert cfg.bankroll == 100 and cfg.telegram_bot_token == "123:abc" and cfg.telegram_chat_id == ""
    assert http.sent == []


def test_setup_public_channel_username(tmp_path):
    path = tmp_path / "config.toml"
    http = FakeTelegram(updates=[])
    cli.cmd_setup(Config(), http, str(path), ask=scripted("", "123:abc", "", "@mypicks", ""))
    assert load_config(path).telegram_chat_id == "@mypicks"


def test_save_settings_keeps_existing_values_and_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[tradebot]\n# my notes\nbankroll = 100.0  # start\nmin_edge = 0.04\n')
    save_settings(path, {"bankroll": 250.0, "telegram_chat_id": "-100", "odds_api_key": 'we"ird'})
    data = tomllib.loads(path.read_text())["tradebot"]
    assert data == {"bankroll": 250.0, "min_edge": 0.04, "telegram_chat_id": "-100", "odds_api_key": 'we"ird'}
    assert "# my notes" in path.read_text()


def test_bot_runs_without_the_claude_library(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # as if `pip install anthropic` was skipped
    assert "pip install anthropic" in thesis._ask("sys", "prompt", Config())
