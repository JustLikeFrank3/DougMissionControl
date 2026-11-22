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


if __name__ == "__main__":
    unittest.main()
