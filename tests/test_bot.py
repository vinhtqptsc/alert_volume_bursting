import os
import socket
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402

VN = bot.VN
NOW = datetime(2026, 10, 28, 11, 0, tzinfo=VN)
TODAY = NOW.date()
ENV = {"TELEGRAM_TOKEN": "SECRET-TOKEN-123", "TELEGRAM_CHAT_ID": "999"}
NOSLEEP = lambda s: None  # noqa: E731


def days_back(n, end=TODAY):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def rows(last_volume, n=21, base=100, end=TODAY, fmt="aware"):
    out = []
    for i, d in enumerate(days_back(n, end)):
        if fmt == "aware":
            t = datetime(d.year, d.month, d.day, 7, tzinfo=VN)
        elif fmt == "naive":
            t = datetime(d.year, d.month, d.day, 7)
        else:
            t = d.isoformat()
        out.append({"time": t, "close": 10.0, "volume": last_volume if i == n - 1 else base})
    return out


class Resp:
    def __init__(self, status=200, data=None, bad_json=False):
        self.status_code, self._data, self._bad = status, data, bad_json

    def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._data


OK = lambda mid=7: Resp(200, {"ok": True, "result": {"message_id": mid}})  # noqa: E731


class Poster:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, data=None, timeout=None):
        self.calls.append(dict(data))
        item = self.responses.pop(0) if self.responses else OK()
        if isinstance(item, Exception):
            raise item
        return item


def fetcher(volumes=None, **kw):
    volumes = volumes or {}

    def f(symbol, now):
        v = volumes.get(symbol, 100)
        if isinstance(v, Exception):
            raise v
        return rows(v, **kw)
    return f


class NoNetwork(unittest.TestCase):
    def setUp(self):
        self._orig = socket.socket.connect
        def blocked(*a, **k):
            raise AssertionError("network blocked in tests")
        socket.socket.connect = blocked

    def tearDown(self):
        socket.socket.connect = self._orig


class StrategyTests(NoNetwork):
    def test_threshold_is_unrounded(self):
        self.assertTrue(bot.analyze("CTS", rows(150), TODAY)["is_spike"])
        r = bot.analyze("CTS", rows(149.9), TODAY)
        self.assertFalse(r["is_spike"])
        r = bot.analyze("CTS", rows(149.99), TODAY)   # hiển thị 1.5 nhưng chưa đạt
        self.assertEqual(r["volume_ratio"], 1.5)
        self.assertFalse(r["is_spike"])

    def test_zero_average_gives_zero_ratio(self):
        r = bot.analyze("CTS", rows(500, base=0), TODAY)
        self.assertEqual(r["volume_ratio"], 0)
        self.assertFalse(r["is_spike"])

    def test_window_is_last_21_and_min_15(self):
        self.assertEqual(bot.analyze("CTS", rows(100, n=30), TODAY)["baseline_bars"], 20)
        self.assertEqual(bot.analyze("CTS", rows(100, n=15), TODAY)["baseline_bars"], 14)
        with self.assertRaises(bot.DataError):
            bot.analyze("CTS", rows(100, n=14), TODAY)

    def test_change_pct_uses_previous_close(self):
        data = rows(100)
        data[-2]["close"] = 10.0
        data[-1]["close"] = 10.5
        self.assertEqual(bot.analyze("CTS", data, TODAY)["change_pct"], 5.0)

    def test_unsorted_and_duplicate_rows(self):
        data = rows(300)
        data = list(reversed(data)) + [data[3]]
        self.assertTrue(bot.analyze("CTS", data, TODAY)["is_spike"])

    def test_timestamp_formats(self):
        for fmt in ("aware", "naive", "str"):
            self.assertFalse(bot.analyze("CTS", rows(100, fmt=fmt), TODAY)["stale"], fmt)

    def test_utc_timestamp_maps_to_vn_date(self):
        # 17:00 UTC ngày 27/10 = 00:00 VN ngày 28/10
        self.assertEqual(bot.bar_date(datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc)), date(2026, 10, 28))

    def test_bad_old_row_does_not_kill_symbol(self):
        data = rows(300)
        data[2]["volume"] = float("nan")
        data[3]["close"] = "abc"
        self.assertTrue(bot.analyze("CTS", data, TODAY)["is_spike"])

    def test_stale_last_bar_is_not_a_signal(self):
        r = bot.analyze("CTS", rows(900, end=TODAY - timedelta(days=1)), TODAY)
        self.assertTrue(r["stale"])

    def test_future_bar_is_error(self):
        with self.assertRaises(bot.DataError):
            bot.analyze("CTS", rows(100, end=TODAY + timedelta(days=1)), TODAY)


class ScanAndRunTests(NoNetwork):
    def run_bot(self, fetch, post=None, env=ENV):
        logs = []
        post = post or Poster()
        rc = bot.run(env, fetch, post, NOW, NOSLEEP, logs.append)
        return rc, post, logs

    def test_one_bad_symbol_does_not_cancel_other_alerts(self):
        vols = {s: 300 for s in bot.WATCHLIST}
        vols["VIX"] = RuntimeError("boom https://x?key=SECRET-TOKEN-123")
        rc, post, logs = self.run_bot(fetcher(vols))
        self.assertEqual(rc, 0)
        self.assertEqual(len(post.calls), 1)
        text = post.calls[0]["text"]
        for s in bot.WATCHLIST:
            if s != "VIX":
                self.assertIn(f"<b>{s}</b>", text)
        self.assertIn("Không kiểm tra được: VIX", text)
        self.assertNotIn("SECRET-TOKEN-123", "\n".join(logs))

    def test_fetch_retried_once(self):
        calls = {"n": 0}
        def flaky(symbol, now):
            if symbol == "CTS":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ConnectionError("temporary")
            return rows(300)
        rc, post, _ = self.run_bot(flaky)
        self.assertEqual(rc, 0)
        self.assertIn("<b>CTS</b>", post.calls[0]["text"])

    def test_failure_with_no_signals_is_red(self):
        vols = {"VIX": RuntimeError("x")}
        rc, post, _ = self.run_bot(fetcher(vols))
        self.assertEqual(rc, 1)
        self.assertEqual(post.calls, [])

    def test_all_failed_is_red_and_tries_to_notify(self):
        def dead(symbol, now):
            raise RuntimeError("down")
        rc, post, _ = self.run_bot(dead)
        self.assertEqual(rc, 1)
        self.assertEqual(len(post.calls), 1)
        self.assertIn("không lấy được dữ liệu", post.calls[0]["text"])

    def test_holiday_all_stale_is_green_and_silent(self):
        rc, post, logs = self.run_bot(fetcher(end=TODAY - timedelta(days=1)))
        self.assertEqual(rc, 0)
        self.assertEqual(post.calls, [])
        self.assertTrue(any("không phải 'không có tín hiệu'" in l for l in logs))

    def test_no_signal_is_green_and_silent(self):
        rc, post, logs = self.run_bot(fetcher())
        self.assertEqual(rc, 0)
        self.assertEqual(post.calls, [])
        self.assertIn("Không có mã đạt điều kiện.", logs)

    def test_message_shows_vietnam_time(self):
        utc_now = datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc)  # 11:00 VN
        post = Poster()
        bot.run(ENV, fetcher({"SSI": 400}), post, utc_now, NOSLEEP, lambda m: None)
        self.assertIn("28/10 11:00 VN", post.calls[0]["text"])

    def test_missing_config_is_red_without_network(self):
        rc, post, _ = self.run_bot(fetcher({"SSI": 400}), env={})
        self.assertEqual(rc, 1)
        self.assertEqual(post.calls, [])

    def test_no_dedupe_state_so_next_run_alerts_again(self):
        post = Poster()
        for _ in range(2):
            bot.run(ENV, fetcher({"SSI": 400}), post, NOW, NOSLEEP, lambda m: None)
        self.assertEqual(len(post.calls), 2)

    def test_next_day_alerts_after_failed_send_yesterday(self):
        bad = Poster(Resp(500), Resp(500), Resp(500))
        rc1 = bot.run(ENV, fetcher({"SSI": 400}), bad, NOW, NOSLEEP, lambda m: None)
        self.assertEqual(rc1, 1)
        tomorrow = NOW + timedelta(days=1)
        good = Poster()
        f = lambda s, n: rows(400 if s == "SSI" else 100, end=tomorrow.date())  # noqa: E731
        rc2 = bot.run(ENV, f, good, tomorrow, NOSLEEP, lambda m: None)
        self.assertEqual(rc2, 0)
        self.assertEqual(len(good.calls), 1)

    def test_escalation_is_visible_in_later_run(self):
        post = Poster()
        bot.run(ENV, fetcher({"SSI": 160}), post, NOW, NOSLEEP, lambda m: None)
        bot.run(ENV, fetcher({"SSI": 600}), post, NOW + timedelta(hours=3), NOSLEEP, lambda m: None)
        self.assertIn("x1.6", post.calls[0]["text"])
        self.assertIn("x6.0", post.calls[1]["text"])


class TelegramTests(NoNetwork):
    def send(self, post, text="hi"):
        logs = []
        ok = bot.send_telegram(post, "SECRET-TOKEN-123", "999", text, NOSLEEP, logs.append)
        return ok, logs

    def test_success_requires_ok_and_message_id(self):
        self.assertTrue(self.send(Poster(OK()))[0])

    def test_http_200_alone_is_not_success(self):
        for data in ({"ok": False}, {"ok": True}, {"ok": True, "result": {}},
                     {"ok": True, "result": {"message_id": True}},
                     {"ok": True, "result": {"message_id": 0}},
                     {"ok": True, "result": {"message_id": "5"}}, [], None):
            post = Poster(*[Resp(200, data)] * 3)
            self.assertFalse(self.send(post)[0], data)

    def test_invalid_json_not_success(self):
        self.assertFalse(self.send(Poster(*[Resp(200, bad_json=True)] * 3))[0])

    def test_retry_on_timeout_then_success(self):
        post = Poster(TimeoutError("t"), OK())
        self.assertTrue(self.send(post)[0])
        self.assertEqual(len(post.calls), 2)

    def test_retry_on_5xx_and_429(self):
        post = Poster(Resp(500), Resp(429, {"ok": False, "parameters": {"retry_after": 1}}), OK())
        self.assertTrue(self.send(post)[0])
        self.assertEqual(len(post.calls), 3)

    def test_gives_up_after_three_attempts(self):
        post = Poster(TimeoutError(), TimeoutError(), TimeoutError(), OK())
        self.assertFalse(self.send(post)[0])
        self.assertEqual(len(post.calls), 3)

    def test_permanent_errors_not_retried(self):
        post = Poster(Resp(401, {"ok": False, "description": "Unauthorized"}), OK())
        self.assertFalse(self.send(post)[0])
        self.assertEqual(len(post.calls), 1)

    def test_html_parse_error_falls_back_to_plain_text(self):
        err = Resp(400, {"ok": False, "description": "Bad Request: can't parse entities"})
        post = Poster(err, OK())
        self.assertTrue(self.send(post, "<b>CTS</b> x2")[0])
        self.assertNotIn("parse_mode", post.calls[1])
        self.assertEqual(post.calls[1]["text"], "CTS x2")

    def test_token_never_logged(self):
        ok, logs = self.send(Poster(TimeoutError("https://api.telegram.org/botSECRET-TOKEN-123/x"),
                                    Resp(500), Resp(500)))
        self.assertFalse(ok)
        self.assertNotIn("SECRET-TOKEN-123", "\n".join(logs))

    def test_long_message_is_split(self):
        text = "\n".join(f"line {i} " + "x" * 90 for i in range(100))
        chunks = bot.split_message(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= bot.MAX_TELEGRAM_CHARS for c in chunks))
        self.assertEqual("".join(chunks), text)
        post = Poster()
        self.assertTrue(bot.deliver(post, "t", "c", text, NOSLEEP, lambda m: None))
        self.assertEqual(len(post.calls), len(chunks))


class DedupeTests(NoNetwork):
    """Chống lặp theo mức tăng, luôn fail-open."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state", "state.json")
        self.env = dict(ENV, VOLUME_ALERT_STATE=self.path)

    def go(self, volumes, post, when=NOW, env=None, fetch=None):
        logs = []
        f = fetch or (lambda s, n: rows(volumes.get(s, 100), end=when.date()))
        rc = bot.run(env or self.env, f, post, when, NOSLEEP, logs.append)
        return rc, logs

    def state(self):
        import json
        with open(self.path, encoding="utf-8") as h:
            return json.load(h)

    def test_same_ratio_not_repeated_but_state_saved_after_confirm(self):
        post = Poster()
        self.assertEqual(self.go({"SSI": 160}, post)[0], 0)
        self.assertEqual(self.state()["last"], {"SSI": 1.6})
        rc, logs = self.go({"SSI": 160}, post, NOW + timedelta(hours=3))
        self.assertEqual(rc, 0)
        self.assertEqual(len(post.calls), 1)
        self.assertTrue(any("chưa tăng đủ 50%" in l for l in logs))

    def test_realert_when_ratio_up_50_percent_inclusive(self):
        post = Poster()
        self.go({"SSI": 200}, post)                       # x2.0
        self.go({"SSI": 299}, post, NOW + timedelta(hours=1))   # x2.99 < 3.0: im lặng
        self.assertEqual(len(post.calls), 1)
        self.go({"SSI": 300}, post, NOW + timedelta(hours=2))   # x3.0: báo lại
        self.assertEqual(len(post.calls), 2)
        self.assertIn("↑ tăng từ x2.0", post.calls[1]["text"])
        self.assertEqual(self.state()["last"], {"SSI": 3.0})

    def test_new_symbol_always_alerts_even_if_other_suppressed(self):
        post = Poster()
        self.go({"SSI": 200}, post)
        self.go({"SSI": 200, "VND": 180}, post, NOW + timedelta(hours=1))
        self.assertEqual(len(post.calls), 2)
        text = post.calls[1]["text"]
        self.assertIn("<b>VND</b>", text)
        self.assertNotIn("<b>SSI</b>", text)
        self.assertEqual(self.state()["last"], {"SSI": 2.0, "VND": 1.8})

    def test_new_day_ignores_old_state(self):
        post = Poster()
        self.go({"SSI": 200}, post)
        self.go({"SSI": 200}, post, NOW + timedelta(days=1))
        self.assertEqual(len(post.calls), 2)

    def test_corrupt_or_odd_state_fails_open(self):
        os.makedirs(os.path.dirname(self.path))
        for content in ("{not json", "[]", '{"date": "2026-10-28", "last": {"SSI": "x", "ZZZ": 9, "VND": true}}', ""):
            with open(self.path, "w", encoding="utf-8") as h:
                h.write(content)
            post = Poster()
            rc, _ = self.go({"SSI": 200}, post)
            self.assertEqual(rc, 0, content)
            self.assertEqual(len(post.calls), 1, content)

    def test_failed_send_does_not_record_state(self):
        bad = Poster(Resp(500), Resp(500), Resp(500))
        self.assertEqual(self.go({"SSI": 200}, bad)[0], 1)
        self.assertFalse(os.path.exists(self.path))
        good = Poster()
        self.assertEqual(self.go({"SSI": 200}, good, NOW + timedelta(hours=1))[0], 0)
        self.assertEqual(len(good.calls), 1)

    def test_unwritable_state_still_sends_and_returns_ok(self):
        blocker = os.path.join(self.dir, "file")
        open(blocker, "w").close()
        env = dict(ENV, VOLUME_ALERT_STATE=os.path.join(blocker, "x", "state.json"))
        post = Poster()
        rc, logs = self.go({"SSI": 200}, post, env=env)
        self.assertEqual(rc, 0)
        self.assertEqual(len(post.calls), 1)
        self.assertTrue(any("Không ghi được trạng thái" in l for l in logs))

    def test_no_state_path_means_repeat_every_run(self):
        post = Poster()
        for h in range(3):
            self.go({"SSI": 200}, post, NOW + timedelta(hours=h), env=ENV)
        self.assertEqual(len(post.calls), 3)

    def test_suppressed_plus_data_failure_is_still_red(self):
        post = Poster()
        self.go({"SSI": 200}, post)
        def f(s, n):
            if s == "VIX":
                raise RuntimeError("x")
            return rows(200 if s == "SSI" else 100)
        rc, _ = self.go({}, post, NOW + timedelta(hours=1), fetch=f)
        self.assertEqual(rc, 1)
        self.assertEqual(len(post.calls), 1)

    def test_failure_note_included_with_fresh_alert_and_state_saved(self):
        post = Poster()
        def f(s, n):
            if s == "VIX":
                raise RuntimeError("x")
            return rows(200 if s == "SSI" else 100)
        rc, _ = self.go({}, post, fetch=f)
        self.assertEqual(rc, 0)
        self.assertIn("Không kiểm tra được: VIX", post.calls[0]["text"])
        self.assertEqual(self.state()["last"], {"SSI": 2.0})


class FetchRowsTests(NoNetwork):
    """fetch_rows với vnstock giả (không cài, không gọi mạng)."""

    def install_fake(self, ohlcv):
        import types
        fn = ohlcv
        class Eq:
            def __init__(self, sym): self.sym = sym
            ohlcv = staticmethod(fn)
        class Market:
            def equity(self, sym): return Eq(sym)
        mod = types.ModuleType("vnstock")
        mod.Market = Market
        self._saved = sys.modules.get("vnstock")
        sys.modules["vnstock"] = mod
        self.addCleanup(self.restore)

    def restore(self):
        if self._saved is None:
            sys.modules.pop("vnstock", None)
        else:
            sys.modules["vnstock"] = self._saved

    def frame(self, **attrs):
        import pandas as pd
        df = pd.DataFrame(rows(300))
        df.columns = [c.title() for c in df.columns]
        df.attrs.update(attrs)
        return df

    def test_passes_interval_and_normalises_columns(self):
        seen = {}
        def ohlcv(start, end, interval=None):
            seen["interval"] = interval
            return self.frame(interval="1D", symbol="CTS")
        self.install_fake(ohlcv)
        data = bot.fetch_rows("CTS", NOW)
        self.assertEqual(seen["interval"], "1D")
        self.assertTrue(bot.analyze("CTS", data, TODAY)["is_spike"])

    def test_old_vnstock_without_interval_kwarg(self):
        def ohlcv(start, end):
            return self.frame()
        self.install_fake(ohlcv)
        self.assertTrue(bot.analyze("CTS", bot.fetch_rows("CTS", NOW), TODAY)["is_spike"])

    def test_non_daily_interval_rejected(self):
        self.install_fake(lambda start, end, interval=None: self.frame(interval="5m"))
        with self.assertRaises(bot.DataError):
            bot.fetch_rows("CTS", NOW)

    def test_time_in_index_is_supported(self):
        import pandas as pd
        def ohlcv(start, end, interval=None):
            df = pd.DataFrame(rows(300)).set_index("time")
            df.index.name = "time"
            return df
        self.install_fake(ohlcv)
        self.assertTrue(bot.analyze("CTS", bot.fetch_rows("CTS", NOW), TODAY)["is_spike"])

    def test_empty_frame_is_error(self):
        import pandas as pd
        self.install_fake(lambda start, end, interval=None: pd.DataFrame())
        with self.assertRaises(bot.DataError):
            bot.fetch_rows("CTS", NOW)


if __name__ == "__main__":
    unittest.main()
