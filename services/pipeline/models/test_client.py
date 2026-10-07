import unittest

from models.client import Client, ModelError
from models.config import Profile
from models.fake_server import FakeServer


def profile(base_url, dims=4, **overrides):
    values = dict(name="t", llm_base_url=base_url, llm_model="chat-x", llm_key_env="TEST_KEY",
                  embedding_base_url=base_url, embedding_model="emb-x", embedding_dimensions=dims,
                  embedding_key_env="TEST_KEY")
    values.update(overrides)
    return Profile(**values)


def client(server, **kwargs):
    env = {"TEST_KEY": "sk-test"}
    return Client(profile(server.base_url, **kwargs.pop("profile", {})), env, sleep=lambda s: None, **kwargs)


class ChatTests(unittest.TestCase):
    def test_chat_returns_the_reply_and_sends_the_model(self):
        with FakeServer() as server:
            reply = client(server).chat([{"role": "user", "content": "ping"}])
            path, auth, body = server.requests[0]
        self.assertEqual(reply, "pong")
        self.assertEqual((path, auth, body["model"]), ("/v1/chat/completions", "Bearer sk-test", "chat-x"))

    def test_default_request_suits_a_reasoning_model(self):
        with FakeServer() as server:
            client(server).chat([{"role": "user", "content": "ping"}])
            body = server.requests[0][2]
        self.assertEqual(body["max_completion_tokens"], 1024)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)

    def test_an_explicit_token_budget_is_passed_through(self):
        with FakeServer() as server:
            c = client(server)
            c.chat([{"role": "user", "content": "ping"}], max_completion_tokens=50)
            c.chat([{"role": "user", "content": "ping"}], max_tokens=7)
            explicit, legacy = server.requests[0][2], server.requests[1][2]
        self.assertEqual(explicit["max_completion_tokens"], 50)
        self.assertNotIn("max_tokens", explicit)
        self.assertEqual(legacy["max_tokens"], 7)
        self.assertNotIn("max_completion_tokens", legacy)


class EmbedTests(unittest.TestCase):
    def test_vectors_come_back_in_input_order_and_dimensions_are_requested(self):
        with FakeServer(dims=4) as server:
            vectors = client(server).embed(["a", "abcd"])
            body = server.requests[0][2]
        self.assertEqual(vectors[0][0], 0.1)
        self.assertEqual(vectors[1][0], 0.4)
        self.assertEqual((body["model"], body["dimensions"], body["input"]), ("emb-x", 4, ["a", "abcd"]))

    def test_dimensions_are_omitted_when_the_profile_says_so(self):
        with FakeServer() as server:
            client(server, profile={"send_dimensions": False}).embed(["a"])
            self.assertNotIn("dimensions", server.requests[0][2])

    def test_only_queries_get_the_template(self):
        with FakeServer() as server:
            c = client(server, profile={"query_template": "Q[{task}]: {query}"})
            c.embed(["a document"])
            c.embed(["a question"], is_query=True, task="find it")
            docs, query = server.requests[0][2]["input"], server.requests[1][2]["input"]
        self.assertEqual(docs, ["a document"])
        self.assertEqual(query, ["Q[find it]: a question"])

    def test_a_wrong_vector_size_is_an_error(self):
        with FakeServer(dims=3) as server:
            with self.assertRaisesRegex(ModelError, "3 dimensions.*expects 4"):
                client(server).embed(["a"])

    def test_large_inputs_are_sent_in_batches(self):
        with FakeServer() as server:
            vectors = client(server).embed([f"t{i}" for i in range(130)])
            self.assertEqual([len(r[2]["input"]) for r in server.requests], [64, 64, 2])
        self.assertEqual(len(vectors), 130)


class FailureTests(unittest.TestCase):
    def test_transient_errors_are_retried(self):
        with FakeServer(fail=[429, 503]) as server:
            self.assertEqual(client(server).chat([{"role": "user", "content": "x"}]), "pong")
            self.assertEqual(len(server.requests), 3)

    def test_retries_run_out(self):
        with FakeServer(fail=[503] * 10) as server:
            with self.assertRaisesRegex(ModelError, "HTTP 503"):
                client(server, retries=2).chat([{"role": "user", "content": "x"}])
            self.assertEqual(len(server.requests), 3)

    def test_a_bad_key_fails_at_once_and_is_never_echoed(self):
        with FakeServer() as server:
            c = Client(profile(server.base_url), {"TEST_KEY": "sk-WRONG"}, sleep=lambda s: None)
            with self.assertRaises(ModelError) as raised:
                c.chat([{"role": "user", "content": "x"}])
            self.assertEqual(len(server.requests), 1)
        self.assertIn("HTTP 401", str(raised.exception))
        self.assertNotIn("sk-WRONG", str(raised.exception))

    def test_an_unreachable_server_is_a_model_error(self):
        c = Client(profile("http://127.0.0.1:9/v1"), {"TEST_KEY": "k"}, retries=0, timeout=2, sleep=lambda s: None)
        with self.assertRaisesRegex(ModelError, "could not be reached"):
            c.embed(["a"])


if __name__ == "__main__":
    unittest.main()
