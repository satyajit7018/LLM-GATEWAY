"""Canonical Step 6 load test using Locust (optional).

Install + run:
    pip install locust
    locust -f locustfile.py --host http://127.0.0.1:8000
then open http://localhost:8089 and start a swarm. Set the cache_mode env below
to compare configs (none / exact / semantic).

For a zero-install comparison table, use `python -m scripts.loadtest` instead.
"""
import os
import random

from locust import HttpUser, between, task

CACHE_MODE = os.getenv("CACHE_MODE", "semantic")

POPULAR = [
    "What is the capital of France?",
    "Explain what a load balancer does.",
    "Give me three uses for Redis.",
]
NEAR_DUPES = [
    "capital of France?",
    "what's the capital of France",
    "three uses for Redis please",
]


class GatewayUser(HttpUser):
    wait_time = between(0.05, 0.25)

    @task(6)
    def popular(self):
        self._ask(random.choice(POPULAR))

    @task(3)
    def near_dupe(self):
        self._ask(random.choice(NEAR_DUPES))

    @task(1)
    def unique(self):
        self._ask(f"Fact about the number {random.randint(0, 100000)}?")

    def _ask(self, prompt):
        self.client.post(
            "/generate", params={"cache_mode": CACHE_MODE}, json={"prompt": prompt}
        )
