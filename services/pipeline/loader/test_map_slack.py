import json
import unittest
from pathlib import Path

from loader.mappers import MapContext, Reject, Skip, map_record
from loader.mappers.slack import map_slack

ROOT = Path(__file__).resolve().parents[3]
CTX = MapContext("brandon-dev")
CHANNEL = {"id": "C012AB3CD", "name": "dev", "is_channel": True, "created": 1717000000}
USER = {"id": "U012ABCDEF", "name": "octo", "real_name": "Octo Cat", "profile": {"email": "octo@example.com"}}
TEAM = {"id": "T012WORKSPACE", "name": "Acme", "domain": "acme"}
TOP = {"type": "message", "channel": "C012AB3CD", "user": "U012ABCDEF", "text": "Shared intake is type plus body.",
       "ts": "1717000001.000200", "thread_ts": "1717000001.000200"}
REPLY = {"type": "message", "channel": "C012AB3CD", "user": "U012ABCDEF", "text": "Agreed.",
         "ts": "1717000002.000300", "thread_ts": "1717000001.000200"}


def edge_set(mapped):
    return {(e.type, e.src_key, e.dst_key) for e in mapped.edges}


class SlackMapperTests(unittest.TestCase):
    def test_channel_and_user(self):
        m = map_slack(CTX, CHANNEL)
        self.assertEqual([(n.label, n.key, n.props["name"]) for n in m.nodes], [("Container", "C012AB3CD", "dev")])
        m = map_slack(CTX, USER)
        person = next(n for n in m.nodes if n.label == "Person")
        self.assertEqual((person.key, person.props["email"]), ("slack:U012ABCDEF", "octo@example.com"))

    def test_top_level_message(self):
        m = map_slack(CTX, TOP)
        msg = next(n for n in m.nodes if n.label == "Message")
        self.assertEqual(msg.key, "C012AB3CD:1717000001.000200")
        self.assertTrue(msg.props["created_at"].startswith("2024-05-29"))
        edges = edge_set(m)
        self.assertIn(("PART_OF", msg.key, "C012AB3CD:1717000001.000200"), edges)
        self.assertIn(("CONTAINS", "C012AB3CD", "C012AB3CD:1717000001.000200"), edges)
        self.assertIn(("AUTHORED", "slack:U012ABCDEF", msg.key), edges)
        self.assertNotIn("REPLIES_TO", {e.type for e in m.edges})
        container = next(n for n in m.nodes if n.label == "Container")
        self.assertNotIn("name", container.props)  # a stub must not overwrite the real channel name

    def test_reply_points_at_parent_and_shares_the_thread(self):
        m = map_slack(CTX, REPLY)
        edges = edge_set(m)
        self.assertIn(("REPLIES_TO", "C012AB3CD:1717000002.000300", "C012AB3CD:1717000001.000200"), edges)
        self.assertIn(("PART_OF", "C012AB3CD:1717000002.000300", "C012AB3CD:1717000001.000200"), edges)

    def test_skips_and_rejects(self):
        for body in (TEAM, {**TOP, "subtype": "message_changed"}, {"foo": "bar"}):
            with self.assertRaises(Skip):
                map_slack(CTX, body)
        with self.assertRaises(Reject):
            map_slack(CTX, {"type": "message", "ts": "1.0", "text": "no channel"})

    def test_event_callback_fixture_through_the_dispatcher(self):
        envelope = json.loads((ROOT / "packages/ingestion-envelope/fixtures/slack.json").read_text(encoding="utf-8"))
        m = map_record(CTX, envelope)
        self.assertEqual([n.key for n in m.nodes if n.label == "Message"], ["C012AB3CD:1717000001.000200"])

    def test_dispatcher_skips_unsupported_sources(self):
        with self.assertRaises(Skip) as raised:
            map_record(CTX, {"type": "teams", "body": {}})
        self.assertEqual(raised.exception.kind, "unsupported_source")


if __name__ == "__main__":
    unittest.main()
