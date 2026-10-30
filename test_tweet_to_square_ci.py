#!/usr/bin/env python3
"""Test khong goi mang that. Chay: python -m unittest"""
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("TWITTER_USERNAME", "tester")
os.environ.setdefault("BINANCE_SQUARE_OPENAPI_KEY", "test-key")

import requests

import tweet_to_square_ci as bot

# Tweet that bi Square tu choi ngay 30/10 (8 ma coin).
EIGHT_COINS = (
    "The Binance hacker has finished dumping $158.25M worth of altcoins.\n\n"
    "7.02M $UNI → $39.63M\n\n217.5M $ENA → $33.93M\n\n7.77T $PEPE → $31.66M\n\n"
    "2.57M $LINK → $26.05M\n\n127,953 $AAVE → $14.27M\n\n"
    "21.85M $ONDO → $6.62M\n\n17.32M $WLD → $6.09M\n\n"
    "All proceeds were swapped into 70,480 $ETH"
)


def coins(text):
    return {m.upper() for m in re.findall(r"\$([A-Za-z][A-Za-z0-9]*)", text)}


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload
        self.text = str(payload)

    def json(self):
        return self.payload


class FakeSquare:
    """Gia lap /content/add: tu choi bai co qua 3 ma coin, nhu Square that."""

    def __init__(self, reject_code=None):
        self.reject_code = reject_code
        self.bodies = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.bodies.append(json)
        if self.reject_code:
            return FakeResponse(200, {"code": self.reject_code, "message": "rejected"})
        if len(coins(json["bodyTextOnly"])) > 3:
            return FakeResponse(200, {"code": "220095",
                                      "message": "Coin pair count exceeds the allowed limit"})
        return FakeResponse(200, {"code": "000000", "data": {"id": "1"}})


class LimitCashtagsTest(unittest.TestCase):
    def test_keeps_first_coins_and_removes_dollar_from_the_rest(self):
        result = bot.limit_cashtags("$UNI $ENA $PEPE $LINK $AAVE", 3)
        self.assertEqual(result, "$UNI $ENA $PEPE LINK AAVE")

    def test_leaves_dollar_amounts_untouched(self):
        result = bot.limit_cashtags("$UNI $ENA $PEPE $LINK → $26.05M", 3)
        self.assertEqual(result, "$UNI $ENA $PEPE LINK → $26.05M")

    def test_repeated_coin_counts_once(self):
        result = bot.limit_cashtags("$ETH $ETH $UNI $ENA $LINK", 3)
        self.assertEqual(result, "$ETH $ETH $UNI $ENA LINK")


class MainFlowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "last_id.txt"
        self.fail = Path(tmp.name) / "fail_count.txt"
        self.state.write_text("100")
        for patcher in (mock.patch.object(bot, "STATE_FILE", self.state),
                        mock.patch.object(bot, "FAIL_FILE", self.fail),
                        mock.patch.object(bot.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, square, tweets):
        with mock.patch.object(bot.requests, "post", square.post), \
             mock.patch.object(bot, "fetch_new_tweets", return_value=tweets), \
             mock.patch.object(bot, "upload_one_image", return_value="https://img"):
            return bot.main()

    def test_too_many_coins_posts_with_limited_cashtags(self):
        square = FakeSquare()
        tweets = [{"id": "101", "text": EIGHT_COINS, "_photos": []}]

        self.assertEqual(self.run_main(square, tweets), 0)

        posted = square.bodies[-1]["bodyTextOnly"]
        self.assertEqual(coins(posted), {"UNI", "ENA", "PEPE"})
        self.assertIn("$39.63M", posted)
        self.assertIn("70,480 ETH", posted)
        self.assertEqual(self.state.read_text(), "101")

    def test_too_many_coins_keeps_photos_on_retry(self):
        square = FakeSquare()
        tweets = [{"id": "101", "text": EIGHT_COINS, "_photos": ["https://a/b.jpg"]}]

        self.assertEqual(self.run_main(square, tweets), 0)

        self.assertEqual(square.bodies[-1]["imageList"], ["https://img"])

    def test_other_square_error_is_not_retried_with_limited_cashtags(self):
        square = FakeSquare(reject_code="20013")
        tweets = [{"id": "101", "text": EIGHT_COINS, "_photos": []}]

        self.assertEqual(self.run_main(square, tweets), 1)

        self.assertEqual(len(square.bodies), 1)
        self.assertEqual(self.state.read_text(), "100")
        self.assertEqual(self.fail.read_text(), "101 1")


class FxRetryTest(unittest.TestCase):
    OK = FakeResponse(200, {"results": [{"id": "101", "text": "hello"}],
                            "cursor": {"bottom": None}})
    NOT_FOUND = FakeResponse(404, {"code": 404, "results": []})

    def fetch(self, responses):
        with mock.patch.object(bot.requests, "get", side_effect=responses) as get, \
             mock.patch.object(bot.time, "sleep"):
            try:
                return bot.get_new_tweets_fx("100"), get.call_count
            except bot.FetchError as e:
                return e, get.call_count

    def test_transient_404_is_retried(self):
        result, calls = self.fetch([self.NOT_FOUND, self.NOT_FOUND, self.OK])
        self.assertEqual([t["id"] for t in result], ["101"])
        self.assertEqual(calls, 3)

    def test_connection_error_is_retried(self):
        result, calls = self.fetch([requests.ConnectionError("boom"), self.OK])
        self.assertEqual([t["id"] for t in result], ["101"])
        self.assertEqual(calls, 2)

    def test_persistent_404_still_raises(self):
        result, calls = self.fetch([self.NOT_FOUND] * bot.FX_ATTEMPTS)
        self.assertIsInstance(result, bot.FetchError)
        self.assertEqual(calls, bot.FX_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
