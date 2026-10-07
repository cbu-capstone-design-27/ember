import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from models.client import MAX_BATCH_CHARS, Client, ModelError
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


class ScriptedServer:
    """Answers every POST by calling behave(handler); for responses FakeServer cannot produce."""

    def __init__(self, behave):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.server.hits.append(self.headers.get("Authorization"))
                behave(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.hits = []

    @property
    def hits(self):
        return self._server.hits

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self):
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


def reply(handler, code, body, headers=()):
    handler.send_response(code)
    for name, value in headers:
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


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

    def test_a_batch_is_also_capped_by_total_characters_and_order_is_kept(self):
        half = "x" * (MAX_BATCH_CHARS // 2)
        texts = [half, half, "y", "z" * 10, half]
        with FakeServer() as server:
            vectors = client(server).embed(texts)
            sent = [r[2]["input"] for r in server.requests]
        self.assertEqual(sent, [[half, half], ["y", "z" * 10, half]])
        self.assertEqual([v[0] for v in vectors], [len(t) / 10 for t in texts])

    def test_a_single_oversized_input_is_still_sent_alone(self):
        big = "x" * (MAX_BATCH_CHARS + 5)
        with FakeServer() as server:
            client(server).embed(["a", big, "b"])
            sent = [r[2]["input"] for r in server.requests]
        self.assertEqual(sent, [["a"], [big], ["b"]])


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

    def test_a_dropped_connection_is_retried_then_reported_as_unreachable(self):
        def hang_up(handler):
            handler.close_connection = True  # no response at all: RemoteDisconnected from getresponse()

        with ScriptedServer(hang_up) as server:
            c = Client(profile(server.base_url), {"TEST_KEY": "k"}, retries=2, sleep=lambda s: None)
            with self.assertRaisesRegex(ModelError, "could not be reached"):
                c.embed(["a"])
            self.assertEqual(len(server.hits), 3)

    def test_a_truncated_body_is_retried_then_reported_as_unreachable(self):
        def short_body(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            handler.wfile.write(b'{"data"')
            handler.close_connection = True  # IncompleteRead while reading

        with ScriptedServer(short_body) as server:
            c = Client(profile(server.base_url), {"TEST_KEY": "k"}, retries=1, sleep=lambda s: None)
            with self.assertRaisesRegex(ModelError, "could not be reached"):
                c.embed(["a"])
            self.assertEqual(len(server.hits), 2)

    def test_a_connection_that_recovers_succeeds(self):
        calls = []

        def flaky(handler):
            calls.append(1)
            if len(calls) == 1:
                handler.close_connection = True
            else:
                reply(handler, 200, b'{"choices": [{"message": {"content": "ok"}}]}')

        with ScriptedServer(flaky) as server:
            c = Client(profile(server.base_url), {"TEST_KEY": "k"}, sleep=lambda s: None)
            self.assertEqual(c.chat([{"role": "user", "content": "x"}]), "ok")

    def test_bytes_that_are_not_utf8_and_json_that_is_not_an_object_are_model_errors(self):
        for body, message in [(bytes([0xFF, 0xFE]), "not JSON"), (b"{oops", "not JSON"), (b"[1, 2]", "not an object"),
                              (b"null", "not an object")]:
            with self.subTest(body=body), ScriptedServer(lambda h, b=body: reply(h, 200, b)) as server:
                c = Client(profile(server.base_url), {"TEST_KEY": "k"}, sleep=lambda s: None)
                with self.assertRaisesRegex(ModelError, message):
                    c.chat([{"role": "user", "content": "x"}])
                self.assertEqual(len(server.hits), 1)

    def test_a_key_split_across_the_500_character_cut_is_still_redacted(self):
        key = "sk-SECRETKEY1234567890"
        body = ("e" * 495 + key + "tail").encode("utf-8")
        with ScriptedServer(lambda h: reply(h, 400, body)) as server:
            c = Client(profile(server.base_url), {"TEST_KEY": key}, sleep=lambda s: None)
            with self.assertRaises(ModelError) as raised:
                c.chat([{"role": "user", "content": "x"}])
        message = str(raised.exception)
        self.assertIn("HTTP 400", message)
        self.assertNotIn("sk-SECRET", message)
        self.assertNotIn("SECRETKEY", message)
        self.assertIn("***", message)

    def test_a_redirect_is_not_followed_and_the_key_is_not_forwarded(self):
        with ScriptedServer(lambda h: reply(h, 200, b"{}")) as target:
            location = target.base_url + "/embeddings"
            with ScriptedServer(lambda h: reply(h, 302, b"", [("Location", location)])) as origin:
                c = Client(profile(origin.base_url), {"TEST_KEY": "sk-test"}, sleep=lambda s: None)
                with self.assertRaisesRegex(ModelError, "HTTP 302"):
                    c.embed(["a"])
                self.assertEqual(len(origin.hits), 1)
            self.assertEqual(target.hits, [])


if __name__ == "__main__":
    unittest.main()
