#!/usr/bin/env python3
"""Tests for douglas-chatgpt.py: the MCP exchange, the secret path, and that
ChatGPT's conversation carries the persona and nothing of the owner's."""

import importlib.util
import json
import os
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["CHATGPT_SECRET"] = "s" * 30
spec = importlib.util.spec_from_file_location("dc", os.path.join(HERE, "douglas-chatgpt.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)


class Bridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seen = []

        def fake(msgs):
            cls.seen.append(msgs)
            return "<think>hm</think>Hello, other robot."
        dc.ask.__defaults__ = (fake,)
        cls.srv = dc.ThreadingHTTPServer(("127.0.0.1", 0), dc.Handler)
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def post(self, path, body, sid=None):
        h = {"Content-Type": "application/json"}
        if sid:
            h["Mcp-Session-Id"] = sid
        req = urllib.request.Request(self.url + path, json.dumps(body).encode(), h)
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None), r.headers.get("Mcp-Session-Id")

    def test_wrong_path_is_404(self):
        for p in ("/mcp", "/" + "t" * 30 + "/mcp", "/"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.post(p, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
            self.assertEqual(e.exception.code, 404)

    def test_conversation(self):
        p = f"/{'s' * 30}/mcp"
        code, r, sid = self.post(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertTrue(sid)
        code, r, _ = self.post(p, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        self.assertEqual(code, 202)
        _, r, _ = self.post(p, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
        self.assertEqual([t["name"] for t in r["result"]["tools"]], ["ask_douglas"])
        for i, text in enumerate(("hi", "again")):
            _, r, _ = self.post(p, {"jsonrpc": "2.0", "id": 3 + i, "method": "tools/call",
                                    "params": {"name": "ask_douglas",
                                               "arguments": {"message": text}}}, sid)
            self.assertFalse(r["result"]["isError"])
            self.assertEqual(r["result"]["content"][0]["text"], "Hello, other robot.")
        msgs = self.seen[-1]
        self.assertEqual(msgs[0]["content"], dc.PERSONA)
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])
        self.assertNotIn("Frank", json.dumps(msgs))

    def test_too_long(self):
        text, err = dc.ask(None, {"message": "x" * (dc.MESSAGE_MAX + 1)})
        self.assertTrue(err)


class Himself(unittest.TestCase):
    """With the pipeline's line set, ChatGPT talks to Douglas himself."""

    @classmethod
    def setUpClass(cls):
        cls.seen, cls.answer = [], {"code": 200}

        class Line(dc.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.seen.append((self.path, self.headers.get("Authorization"), body))
                code = cls.answer["code"]
                out = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Your owner takes his coffee black."}}]}
                                 if code == 200 else
                                 {"error": {"message": "Douglas is busy (thinking): try again in a moment"}}).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        cls.line = dc.ThreadingHTTPServer(("127.0.0.1", 0), Line)
        threading.Thread(target=cls.line.serve_forever, daemon=True).start()
        cls.was = (dc.ask.__defaults__, dc.LINE_URL, dc.LINE_TOKEN)
        dc.ask.__defaults__ = (dc.complete,)
        dc.LINE_URL, dc.LINE_TOKEN = f"http://127.0.0.1:{cls.line.server_port}/v1", "the-connectors-own-token"
        dc.RECENT.clear()

    @classmethod
    def tearDownClass(cls):
        dc.ask.__defaults__, dc.LINE_URL, dc.LINE_TOKEN = cls.was
        cls.line.shutdown()

    def test_it_is_douglas_who_answers(self):
        import contextlib
        import io
        sid = dc.SESSIONS.new()
        journal = io.StringIO()
        with contextlib.redirect_stdout(journal):
            first = dc.ask(sid, {"message": "How does your owner take his coffee?"})
            second = dc.ask(sid, {"message": "And his tea?"})
        self.assertEqual(first, ("Your owner takes his coffee black.", False))
        self.assertFalse(second[1])
        path, auth, body = self.seen[-1]
        # Over the pipeline's line, with this program's own token, and no persona of its own:
        # the session's conversation so far and the message, and nothing else.
        self.assertEqual((path, auth), ("/v1/chat/completions", "Bearer the-connectors-own-token"))
        self.assertEqual((body["model"], body["stream"]), ("douglas", False))
        self.assertEqual([(m["role"], m["content"]) for m in body["messages"]],
                         [("user", "How does your owner take his coffee?"), ("assistant", "Your owner takes his coffee black."),
                          ("user", "And his tea?")])
        # What was said is in Douglas's own record, not in this program's journal.
        self.assertIn("chatgpt and douglas: 12 characters asked", journal.getvalue())
        self.assertNotIn("coffee", journal.getvalue())
        self.assertNotIn("tea", journal.getvalue())

    def test_the_tool_says_what_it_is_now(self):
        self.assertIn("what he knows and remembers", dc.tool()["description"])
        self.assertIn("cannot look anything up", dc.tool()["description"])
        self.assertEqual(dc.tool()["name"], "ask_douglas")
        self.assertEqual(dc.tool()["inputSchema"], dc.TOOL["inputSchema"])
        r, _ = dc.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, None)
        self.assertEqual(r["result"]["tools"][0]["description"], dc.HIMSELF)

    def test_busy_too_long_and_not_answering(self):
        self.answer["code"] = 503
        self.assertEqual(dc.ask(None, {"message": "Are you there?"}),
                         ("Douglas is busy (thinking): try again in a moment.", True))
        self.answer["code"] = 401                 # the line does not know this program's token
        self.assertEqual(dc.ask(None, {"message": "Are you there?"}), ("Douglas is not answering right now.", True))
        self.answer["code"] = 200
        n = len(self.seen)
        text, err = dc.ask(None, {"message": "x" * (dc.LINE_MAX + 1)})
        self.assertTrue(err)
        self.assertIn(str(dc.LINE_MAX), text)
        self.assertEqual(len(self.seen), n)       # not sent at all

    def test_without_the_line_it_is_as_it_was(self):
        was, dc.LINE_TOKEN = dc.LINE_TOKEN, ""
        try:
            self.assertFalse(dc.on_the_line())
            self.assertEqual(dc.tool(), dc.TOOL)
        finally:
            dc.LINE_TOKEN = was


if __name__ == "__main__":
    unittest.main()
