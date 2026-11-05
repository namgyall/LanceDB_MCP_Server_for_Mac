import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class ClientRegistryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        self.registry = server.McpClientRegistry(clock=lambda: self.now)

    def test_initialized_client_is_visible_before_first_tool_call(self):
        self.registry.register(
            "session-1",
            {"name": "copilot", "title": "GitHub Copilot", "version": "1.0"},
        )

        self.assertEqual(self.registry.snapshot()[0]["title"], "GitHub Copilot")

    def test_session_requests_refresh_client_activity(self):
        self.registry.register(
            "session-1",
            {"name": "copilot", "title": "GitHub Copilot", "version": "1.0"},
        )
        original_seen = self.registry.snapshot()[0]["last_seen"]
        self.now += timedelta(minutes=4)
        self.registry.touch("session-1")

        self.assertNotEqual(self.registry.snapshot()[0]["last_seen"], original_seen)

    def test_disconnected_session_is_removed(self):
        self.registry.register(
            "session-1",
            {"name": "copilot", "title": "GitHub Copilot", "version": "1.0"},
        )
        self.registry.remove("session-1")

        self.assertEqual(self.registry.snapshot(), [])

    def test_idle_client_expires_after_five_minutes(self):
        self.registry.register(
            "session-1",
            {"name": "copilot", "title": "GitHub Copilot", "version": "1.0"},
        )
        self.now += timedelta(seconds=server.CLIENT_RECENCY_SECONDS + 1)

        self.assertEqual(self.registry.snapshot(), [])

    def test_initialize_without_name_is_not_reported(self):
        self.registry.register("session-1", {"version": "1.0"})

        self.assertEqual(self.registry.snapshot(), [])


if __name__ == "__main__":
    unittest.main()
