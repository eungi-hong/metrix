"""A market-open spike: many users on five hot tickers, some asking questions.

Run it against a LOCAL API only (never production); docs/PERFORMANCE.md has
the setup, the commands and the results.

    locust -f loadtest/locustfile.py --headless -u 50 -r 10 -t 60s \
        --host http://127.0.0.1:8000 --csv out/after

Each simulated user opens one of the hot tickers the way the web app does
(include_prices=true, limit=50), keeps reading it and the others, and now
and then asks Ask Metrix a question about the one it is on (CHAT_SHARE of
its actions). With REVALIDATE=1 (the default) a user sends If-None-Match
with the ETag it last saw, as a browser does with `cache: "no-cache"`; set
REVALIDATE=0 to measure the server-side cache alone. Against an API without
ETags the header is simply never sent.
"""

from __future__ import annotations

import os
import random

from locust import HttpUser, between, task

HOT = os.environ.get("HOT_TICKERS", "NVDA,AAPL,TSLA,MSFT,AMZN").split(",")
REVALIDATE = os.environ.get("REVALIDATE", "1") == "1"
CHAT_WEIGHT = int(os.environ.get("CHAT_WEIGHT", "1"))
READ_WEIGHT = int(os.environ.get("READ_WEIGHT", "19"))
QUESTIONS = (
    "Why did it move on its biggest day?",
    "What explains the latest move?",
    "Was the last drop company news or the market?",
)


class Trader(HttpUser):
    wait_time = between(0.5, 2.0)

    def on_start(self) -> None:
        self.etags: dict[str, str] = {}
        self.symbol = random.choice(HOT)

    @task(READ_WEIGHT)
    def read_ticker(self) -> None:
        if random.random() < 0.3:
            self.symbol = random.choice(HOT)
        headers = {"Accept": "application/json"}
        if REVALIDATE and self.symbol in self.etags:
            headers["If-None-Match"] = self.etags[self.symbol]
        with self.client.get(
            f"/tickers/{self.symbol}?include_prices=true&limit=50",
            headers=headers,
            name="GET /tickers/[hot]",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 304):
                if "ETag" in response.headers:
                    self.etags[self.symbol] = response.headers["ETag"]
                response.success()
            else:
                response.failure(f"status {response.status_code}")

    @task(CHAT_WEIGHT)
    def ask(self) -> None:
        with self.client.post(
            "/chat",
            json={"ticker": self.symbol, "question": random.choice(QUESTIONS)},
            name="POST /chat",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"status {response.status_code}")
