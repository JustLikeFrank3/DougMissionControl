#!/usr/bin/env python3
"""douglas-work against a stand-in GitHub (bare repositories in a folder, and
a small API server), a stand-in model server, and the kernel's real fence.

    python3 douglas/test_douglas_work.py

The fence needs Landlock 4 or later (Linux 6.7). Where the kernel has less,
the checks that run a job are left out, and the run says so.
"""

import importlib.util
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAILS = 0


def check(name, got, want):
    global FAILS
    ok = got == want
    FAILS += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))


def has(name, text, *parts, never=()):
    """Every one of `parts` is in the text, and none of `never`."""
    missing = [p for p in parts if p not in str(text)] + [f"(unwanted) {p}" for p in never if p in str(text)]
    global FAILS
    FAILS += bool(missing)
    print(f"  {'FAIL' if missing else 'PASS'}  {name}: " + (
        f"missing {missing!r} in {str(text)[:700]!r}" if missing else f"got {str(text)[:110]!r}"))


root = tempfile.mkdtemp(prefix="douglas-work-test-")
remotes = os.path.join(root, "github")
GITENV = dict(os.environ, GIT_AUTHOR_NAME="Ann", GIT_AUTHOR_EMAIL="ann@example.com",
              GIT_COMMITTER_NAME="Ann", GIT_COMMITTER_EMAIL="ann@example.com",
              GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
              GIT_AUTHOR_DATE="2026-11-01T12:00:00", GIT_COMMITTER_DATE="2026-11-01T12:00:00")


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, env=GITENV, check=True, capture_output=True, text=True).stdout


def put(tree, files):
    for rel, text in files.items():
        p = os.path.join(tree, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(text if isinstance(text, bytes) else text.encode())


def publish(owner, name, files):
    """A repository on the stand-in GitHub, and the work tree it was pushed from."""
    tree = os.path.join(root, "trees", owner, name)
    os.makedirs(tree)
    sh("git", "init", "-q", "-b", "main", tree)
    put(tree, files)
    sh("git", "add", "-A", cwd=tree)
    sh("git", "commit", "-q", "-m", "first", cwd=tree)
    os.makedirs(os.path.join(remotes, owner), exist_ok=True)
    sh("git", "clone", "-q", "--bare", tree, os.path.join(remotes, owner, name + ".git"))
    return tree


def push(tree, owner, name, files, message):
    put(tree, files)
    sh("git", "add", "-A", cwd=tree)
    sh("git", "commit", "-q", "-m", message, cwd=tree)
    sh("git", "push", "-q", os.path.join(remotes, owner, name + ".git"), "HEAD:main", cwd=tree)


HELLO = 'def greet(name):\n    return "Hello, " + name\n\n\nif __name__ == "__main__":\n    print(greet("world"))\n'
tree = publish("acme", "jobContextMCP", {
    "README.md": "# jobContextMCP\n\nStories and tone samples, served over MCP.\n",
    "server.py": ("import stories\n\n\ndef load_stories(path):\n    \"\"\"Read every story from a folder.\"\"\"\n"
                  "    return stories.StoryStore(path).all()\n\n\ndef tone_sample(n):\n"
                  "    return load_stories('tone')[n]\n"),
    "tools/stories.py": "class StoryStore:\n    def __init__(self, path):\n        self.path = path\n\n"
                        "    def all(self):\n        return []\n",
    "tests/test_stories.py": "from server import load_stories\n\n\ndef test_load_stories():\n"
                             "    assert load_stories('x') == []\n",
    "hello.py": HELLO,
    "logo.bin": b"\x89PNG\0\0\0binary\0",
    "long.txt": "".join(f"line {i} of the long file, padded out so that it takes some room\n"
                        for i in range(1, 1001)),
    **{f"data/item{i:02d}.txt": f"item {i}\n" for i in range(70)},
})
publish("acme", "kettle", {"boil.py": "def boil():\n    return 100\n"})
publish("acme", "jobboard", {"board.py": "JOBS = []\n"})
publish("acme", "vault", {"secret.py": "KEY = 'not a real one'\n"})

ROWS = [{"name": "jobContextMCP", "private": False, "size": 1200},
        {"name": "kettle", "private": False, "size": 40},
        {"name": "jobboard", "private": False, "size": 40},
        {"name": "huge", "private": False, "size": 900 * 1024},
        {"name": "vault", "private": True, "size": 10}]
for row in ROWS:
    row.update(owner={"login": "acme"}, default_branch="main")
API = {"seen": []}


class Api(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        signed = self.headers.get("Authorization")
        API["seen"].append((self.path, signed))
        code, body = 404, {"message": "Not Found"}
        if self.path.startswith("/users/acme/repos"):
            code, body = 200, [r for r in ROWS if not r["private"]]
        elif self.path.startswith("/user/repos") and signed:
            code, body = 200, ROWS
        else:
            m = re.fullmatch(r"/repos/([^/]+)/([^/]+)", self.path)
            row = m and next((r for r in ROWS if (m[1].lower(), m[2].lower()) == ("acme", r["name"].lower())
                              and (signed or not r["private"])), None)
            if row:
                code, body = 200, row
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ── a stand-in model server: it answers from a script, as a stream ─────────
MODEL = {"script": [], "seen": [], "dropped": 0}


def says(content="", *calls, delay=0.0):
    """One reply of the model's: words, and the tools it calls."""
    return {"content": content, "calls": list(calls), "delay": delay}


class Model(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def gone(self) -> bool:
        ready, _, _ = select.select([self.connection], [], [], 0)
        try:
            return bool(ready) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        MODEL["seen"].append(body)
        reply = MODEL["script"].pop(0) if MODEL["script"] else says("I have nothing more to do.")
        until = time.time() + reply["delay"]
        while time.time() < until:
            if self.gone():                       # dropped: the same answer waits for the next asking
                MODEL["script"].insert(0, reply)
                MODEL["dropped"] += 1
                return
            time.sleep(0.05)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def piece(delta, **more):
            self.wfile.write(b"data: " + json.dumps(dict(more, choices=[{"index": 0, "delta": delta}])).encode()
                             + b"\n\n")
            self.wfile.flush()

        try:
            piece({"role": "assistant", "content": None})
            words = reply["content"]
            for part in (words[:len(words) // 2], words[len(words) // 2:]):
                if part:
                    piece({"content": part})
            for i, (name, args) in enumerate(reply["calls"]):
                args = args if isinstance(args, str) else json.dumps(args)
                piece({"tool_calls": [{"index": i, "id": f"c{len(MODEL['seen'])}{i}", "type": "function",
                                       "function": {"name": name, "arguments": ""}}]})
                for part in (args[:len(args) // 2], args[len(args) // 2:]):
                    piece({"tool_calls": [{"index": i, "function": {"arguments": part}}]})
            piece({}, usage={"prompt_tokens": reply.get("tokens", 900), "completion_tokens": 40})
            self.wfile.write(b"data: [DONE]\n\n")
        except OSError:
            pass


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


voice = os.path.join(root, "voice.json")
os.environ.update(
    WORK_DIR=os.path.join(root, "work"), WORK_JOBS=os.path.join(root, "spool"),
    WORK_SOCKET=os.path.join(root, "work.sock"), WORK_JOB_DIR=os.path.join(root, "job"),
    WORK_GIT_URL="file://" + remotes, WORK_API_URL=f"http://127.0.0.1:{serve(Api)}",
    WORK_OWNER="acme", WORK_GITHUB_TOKEN="", WORK_VOICE_STATE=voice, WORK_MAX_MB="500",
    WORK_CLIENTS="", WORK_STEPS="30", WORK_SECONDS="600", WORK_SLOT="",
    LLM_URL=f"http://127.0.0.1:{serve(Model)}", LLM_MODEL="stand-in")
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("douglas_work", os.path.join(here, "douglas-work.py"))
work = importlib.util.module_from_spec(spec)
spec.loader.exec_module(work)
LOG = []
work.log = LOG.append
m = work.Manager()


def ask(**req):
    return m.call(req)


print("== which repository, and cloning it")
r = ask(op="repos")
has("nothing is cloned at first, and it says where a bare name is looked up", r["text"],
    "No repository is cloned", "acme")
check("and a question about code says so too", ask(op="files"),
      {"ok": False, "text": "No repository is cloned on this machine yet. Clone one first."})
r = ask(op="clone", name="my job context mcp repo")
check("a name as it is spoken finds the repository on the owner's GitHub",
      (r["ok"], r.get("repo"), r.get("private")), (True, "acme/jobContextMCP", False))
has("and what was cloned is said", r["text"], "Cloned acme/jobContextMCP", "public", "77 files",
    "branch main", "2026-11-01: first")
check("it is a bare clone: the code is not laid out as files anybody could run",
      (os.path.isdir(os.path.join(work.REPOS, "acme", "jobContextMCP.git", "objects")),
       os.path.exists(os.path.join(work.REPOS, "acme", "jobContextMCP.git", "hello.py"))), (True, False))
has("a name that could be two is asked about", ask(op="clone", name="job")["text"],
    "Which of these", "jobContextMCP", "jobboard")
has("a repository too large is not cloned", ask(op="clone", name="huge")["text"], "900 MB", "500 MB")
has("one that is not there is said not to be, with what is", ask(op="clone", name="the zebra repo")["text"],
    "no repository like that", "kettle", "Private ones are hidden from me")
has("a private one is not seen without a token", ask(op="clone", name="vault")["text"],
    "no repository like that")
r = ask(op="clone", name="other/thing")
check("an owner and a name that GitHub does not have", (r["ok"], "no such repository" in r["text"]),
      (False, True))
check("and nothing is left behind by a clone that failed",
      os.path.exists(os.path.join(work.REPOS, "other")) and os.listdir(os.path.join(work.REPOS, "other")), [])
has("a name that is no name is turned down", ask(op="clone", name="acme/../../etc")["text"], "no repository")
r = ask(op="clone", name="https://github.com/acme/kettle")
check("an address works as well as a name", (r["ok"], r.get("repo")), (True, "acme/kettle"))
has("cloning one that is here already fetches it instead", ask(op="clone", name="kettle")["text"],
    "cloned already", "Nothing new")
has("both are listed", ask(op="repos")["text"], "acme/jobContextMCP", "acme/kettle", "Fetched")
check("with two here, a question goes to the one last spoken of",
      ask(op="files").get("repo"), "acme/kettle")
m.last = ""
has("and with none spoken of, it is asked which", ask(op="files")["text"], "Which repository",
    "acme/kettle")

print("== reading the code")
r = ask(op="files", repo="jobcontextmcp")
has("a big repository is shown a level at a time", r["text"], "acme/jobContextMCP, branch main: 77 files",
    "data/  (70 files)", "tools/  (1 file)", "tests/  (1 file)", "hello.py", never=["item00.txt"])
has("a folder", ask(op="files", path="tools")["text"], "1 file under tools/", "tools/stories.py")
has("a folder with more than fits says what is left", ask(op="files", path="data/")["text"],
    "70 files under data/", "item00.txt")
has("file names like a word", ask(op="files", find="STOR")["text"], "2 files", "tools/stories.py",
    "tests/test_stories.py")
has("a folder that is not there", ask(op="files", path="nope")["text"], "There is no 'nope'")
r = ask(op="read", path="stories.py")
has("a file is found by its name alone", r["text"], "tools/stories.py in acme/jobContextMCP",
    "1: class StoryStore:", "that is the end of the file")
has("or by part of it", ask(op="read", path="server")["text"], "server.py in", "4: def load_stories(path):")
has("from a line on", ask(op="read", path="hello.py", line=5)["text"], '5: if __name__ == "__main__":',
    never=["1: def greet"])
r = ask(op="read", path="long.txt")
has("a long file is read a part at a time, and says where to go on", r["text"], "1: line 1 of",
    "of 1000. Ask for line")
check("and one answer fits in what Douglas is handed", len(r["text"]) <= 4000, True)
has("a phrase to jump to", ask(op="read", path="long.txt", find="LINE 500 of")["text"],
    "497: line 497", "500: line 500")
has("a phrase that is not there", ask(op="read", path="hello.py", find="zebra")["text"],
    "'zebra' is not in it")
has("a file that is not text", ask(op="read", path="logo.bin")["text"], "is not text")
has("a name that several files have", ask(op="read", path="item")["text"], "Several files", "Say which")
has("a path that climbs out names no file", ask(op="read", path="../../../etc/passwd")["text"],
    "There is no file")
r = ask(op="search", words="load_stories")
has("a search finds the lines", r["text"], "5 lines in 2 files", "server.py:4: def load_stories(path):")
check("what defines the thing comes first, and the tests last",
      [l.split(":")[0] for l in r["text"].splitlines()[1:5]],
      ["server.py", "server.py", "tests/test_stories.py", "tests/test_stories.py"])
has("words that are not one phrase are looked for together", ask(op="search", words="story store path")["text"],
    "not found as one phrase", "tools/stories.py:1: class StoryStore:")
has("nothing found is said plainly", ask(op="search", words="zebra")["text"], "Nothing in acme/jobContextMCP")
has("no file with every word: then wherever any of them is", ask(op="search", words="tone zebra")["text"],
    "any of the words tone, zebra (no file has them all)", "server.py:9: def tone_sample(n):")
r = ask(op="about")
has("what a repository is: its own account of itself and how it is laid out", r["text"],
    "acme/jobContextMCP is public, 77 files on branch main", "Mostly: 71 .txt files, 4 .py files",
    "At the top: data/ (70), tests/ (1), tools/ (1), README.md", "in README.md:",
    "Stories and tone samples, served over MCP.")
r = ask(op="find", words="load_stories")
has("a question about the code gets the best matches shown in their place", r["text"],
    "5 lines in 2 file(s) match 'load_stories'", "server.py, around line 4:", "3: \n4: def load_stories(path):",
    '5:     """Read every story from a folder."""', "tests/test_stories.py, around line 1:",
    "5:     assert load_stories('x') == []", never=["Other lines that match"])
publish("acme", "phrases", {
    "CHANGELOG.md": "- the tone profile is now cached\n", "docs/guide.md": "Ask for the tone profile first.\n",
    "tests/test_tone.py": "def test_tone_profile():\n    pass\n", ".github/notes.md": "tone profile notes\n",
    "app/tone.py": "CACHE = {}\n\n\ndef get_tone_profile():\n    return CACHE\n\n\nx = get_tone_profile()\n",
    "web/tone.js": "export function loadToneProfile() {}\n", "README.md": "<p align=\"center\"><img src=\"b.svg\"/></p>\n\n"
    "[![build](https://example.com/b.svg)](https://example.com)\n\n# Phrases\n\nKeeps a **tone profile** for "
    "[writers](https://example.com/w).\n"})
ROWS.append({"name": "phrases", "private": False, "size": 10, "owner": {"login": "acme"}, "default_branch": "main"})
m.listed.clear()
ask(op="clone", name="phrases")
r = ask(op="search", repo="phrases", words="tone profile")
check("a phrase is looked for as code would spell it too, and the code comes first: what defines the thing, what "
      "uses it, what is written about it, its tests, and last the record of changes",
      [l.split(": ")[0] for l in r["text"].splitlines()[1:-1]],
      ["app/tone.py:4", "web/tone.js:1", "app/tone.py:8", "README.md:7", "docs/guide.md:1", "tests/test_tone.py:1",
       ".github/notes.md:1", "CHANGELOG.md:1"])
has("a README is given in its own words, without its badges and its markup",
    ask(op="about", repo="phrases")["text"], "in README.md:\nPhrases\n\nKeeps a tone profile for writers.",
    never=["<p", "example.com", "**", "build"])
m.last = "acme/jobContextMCP"
r = ask(op="find", words="item")
has("with more matches than can be shown in place, the rest are listed", r["text"],
    "70 lines in 70 file(s) match 'item'", "data/item00.txt, around line 1:", "Other lines that match:",
    "data/item02.txt:1: item 2", "data/item69.txt:1: item 69")
check("and still fits in what Douglas is handed", len(r["text"]) <= 4000, True)
has("words in no line may still be in a file's name", ask(op="find", words="logo")["text"],
    "No line of acme/jobContextMCP matches 'logo'", "File names that do: logo.bin")
has("or nowhere", ask(op="find", words="zebra")["text"], "No file name does either")
check("a question with no words to look for is answered with what the repository is",
      ask(op="find")["text"].startswith("acme/jobContextMCP is public"), True)
m.last = "acme/kettle"
check("the repository a question is about may be named in the person's own words",
      (ask(op="about", said="So what does job context MCP actually do?").get("repo"),
       ask(op="files", said="And what is in it?").get("repo")), ("acme/jobContextMCP", "acme/jobContextMCP"))
m.last = "acme/kettle"
check("a name that names nothing (\"the repo\") leaves it to the one last spoken of, and a wrong name is said to be wrong",
      (ask(op="files", repo="the repo").get("repo"), ask(op="files", repo="it").get("repo"),
       ask(op="files", repo="zebra")["text"][:42]),
      ("acme/kettle", "acme/kettle", "No repository like 'zebra' is cloned here."))
m.last = "acme/jobContextMCP"
has("the history", ask(op="log", count=3)["text"], "Ann: first", "2026-11-01")
push(tree, "acme", "jobContextMCP", {"hello.py": HELLO.replace("world", "everyone")}, "greet everyone")
r = ask(op="update", repo="jobContextMCP")
has("fetching brings in what is new", r["text"], "1 new commit came in", "greet everyone")
has("and that is what is read from then on", ask(op="read", path="hello.py")["text"], 'greet("everyone")')

print("== the door")
srv = work.Server(work.SOCKET, work.Door)
srv.manager, srv.may = m, {os.getuid()}
threading.Thread(target=srv.serve_forever, daemon=True).start()


def door(line: bytes) -> dict:
    s = socket.socket(socket.AF_UNIX)
    s.connect(work.SOCKET)
    s.sendall(line + b"\n")
    got = s.makefile("rb").readline()
    s.close()
    return json.loads(got)


check("a question through the socket", door(b'{"op": "log", "repo": "kettle", "count": 1}')["ok"], True)
has("what is not a question", door(b"clone everything")["text"], "tripped")
has("what is not on offer", door(b'{"op": "delete"}')["text"], "no such thing to ask")
srv.may = {12345}
check("and a caller it does not know is told nothing",
      door(b'{"op": "repos"}'), {"ok": False, "text": "The workshop does not answer that caller."})
srv.may = {os.getuid()}

print("== the access token")
work.TOKEN = "not-a-real-token-0123456789"
seen = {}
real_run = subprocess.run


def spy(cmd, **kw):
    seen.update(cmd=list(cmd), env=dict(kw.get("env") or {}))
    return real_run(cmd, **kw)


subprocess.run = spy
work.git("ls-remote", f"{work.GIT_URL}/acme/kettle.git", auth=True)
subprocess.run = real_run
check("the token is handed to git in its environment, and is on no command line",
      (seen["env"].get("WORK_GITHUB_TOKEN"), work.TOKEN in " ".join(seen["cmd"]),
       f"credential.{work.GIT_URL}.helper" in " ".join(seen["cmd"])), (work.TOKEN, False, True))
subprocess.run = spy
work.git("log", "-1", cwd=m.path("acme", "kettle"))
subprocess.run = real_run
check("and only to a command that fetches", "WORK_GITHUB_TOKEN" in seen["env"], False)
m.listed.clear()
API["seen"].clear()
r = ask(op="clone", name="vault")
check("with a token the private ones are seen, and what is cloned is marked private",
      (r["ok"], r.get("repo"), r.get("private")), (True, "acme/vault", True))
check("it went to GitHub as a header", API["seen"][0],
      ("/user/repos?per_page=100&affiliation=owner&sort=pushed", "Bearer " + work.TOKEN))
has("and reading it says so each time", str(ask(op="read", repo="vault", path="secret.py")),
    "'private': True")
work.TOKEN = ""
m.listed.clear()

LANDLOCK = work.landlock_abi() >= 4
abi, work.landlock_abi = work.landlock_abi, lambda: 3
try:
    work.fence_in(root)
    check("a kernel that cannot refuse the network gets no fence at all", "no error", "an error")
except OSError as e:
    has("a kernel that cannot refuse the network gets no fence at all", e, "cannot refuse network")
work.landlock_abi = abi

if not LANDLOCK:
    print("== NOT RUN: jobs (this kernel has no Landlock 4, so nothing can be fenced in)")
else:
    print("== a job")
    threading.Thread(target=m.watch, daemon=True).start()

    def shown() -> list[str]:
        """What the tools answered, as the model was shown it in its last asking."""
        return [str(x.get("content")) for x in MODEL["seen"][-1]["messages"] if x.get("role") == "tool"]

    def job(task, script, repo="jobContextMCP", during=None, before=None) -> dict:
        MODEL["script"][:] = script
        MODEL["seen"].clear()
        r = ask(op="work", repo=repo, task=task, before=before)
        if not r["ok"]:
            raise AssertionError(r)
        t = threading.Thread(target=lambda: work.Worker(r["job"]).run(), daemon=True)
        t.start()
        if during:
            during(r["job"])
        for _ in range(90):
            ask(op="wait", job=r["job"], seconds=1)
            if r["job"] not in m.jobs:
                break
        t.join(15)
        return next(x for x in m.records() if x["id"] == r["job"])

    def bare_git(*args, repo="jobContextMCP"):
        return work.git(*args, cwd=m.path("acme", repo)).stdout

    rec = job("Make the greeting say Hi instead of Hello.", [
        says("", ("read", {"path": "hello.py"})),
        says("I see it.", ("replace", {"path": "hello.py", "old": '"Hello, "', "new": '"Hi, "'})),
        says("", ("run", {"command": "python3 hello.py"})),
        says("", ("done", {"summary": "Changed the greeting in hello.py and ran it: it prints Hi, everyone."})),
    ], before=["What is in hello.py?", "Make the greeting say Hi instead of Hello.", "x" * 900])
    check("the job is done, with one commit that changes the one file",
          (rec["state"], rec["commits"], rec["files"], rec["step"]), ("done", 1, ["hello.py"], 4))
    check("the model was shown the file, the change, and the command's output",
          (shown()[0].startswith("1: def greet(name):"), shown()[1], "Exit code 0" in shown()[2],
           "Hi, everyone" in shown()[2]), (True, "Changed hello.py.", True, True))
    check("the job itself has no way to the model: what it would ask it writes down, and the manager asks, "
          "with the tools on this program's own list",
          ([t["function"]["name"] for t in MODEL["seen"][0]["tools"]], MODEL["seen"][0]["stream"]),
          (["list", "read", "search", "replace", "write", "run", "done"], True))
    has("it was told the repository, the job and its limits",
        MODEL["seen"][0]["messages"][1]["content"], "acme/jobContextMCP", "hello.py", "tools/",
        "Make the greeting say Hi", "30 steps", never=[".git"])
    has("and what the person said just before, for a job that points back at it, cut to size and without the job itself",
        MODEL["seen"][0]["messages"][1]["content"], "What the person said just before", "- What is in hello.py?",
        "- " + "x" * 600 + "\n", never=["- Make the greeting", "x" * 601])
    has("the change is on a branch of the job's own in the clone",
        bare_git("show", f"refs/heads/{rec['branch']}:hello.py"), 'return "Hi, " + name')
    has("under his name, with the job as its message",
        bare_git("log", "-1", "--format=%an <%ae>%n%s%n%b", "refs/heads/" + rec["branch"]),
        "Douglas <douglas@localhost>", "Douglas: Make the greeting say Hi instead of Hello.",
        "ran it: it prints Hi")
    has("and what GitHub has is as it was", bare_git("show", "refs/remotes/origin/main:hello.py"),
        'return "Hello, " + name')
    check("the branch is named for the job", bool(re.fullmatch(
        r"douglas/make-the-greeting-say-hi-[0-9a-f]{6}", rec["branch"])), True)
    r = ask(op="status")
    has("asked afterwards, the counts are the manager's and the account is marked as the worker's own",
        r["text"], "finished after", "4 steps", "1 file changed, 1 insertion(+), 1 deletion(-)",
        rec["branch"], "Nothing has been pushed", "Files: hello.py",
        "its own words, not checked", "it prints Hi, everyone")
    check("and in pieces too, for the line the person is shown",
          ({k: r.get(k) for k in ("state", "repo", "commits", "files", "stat", "branch", "error")},
           r["summary"][:20]),
          ({"state": "done", "repo": "acme/jobContextMCP", "commits": 1, "files": ["hello.py"],
            "stat": "1 file changed, 1 insertion(+), 1 deletion(-)", "branch": rec["branch"], "error": ""},
           "Changed the greeting"))
    check("waiting on a job that is over comes straight back with what became of it",
          ask(op="wait", job=rec["id"], seconds=20).get("state"), "done")
    r = ask(op="changes")
    has("what changed can be read", r["text"], "hello.py | 2 +-", '-    return "Hello, " + name',
        '+    return "Hi, " + name', "not pushed")
    has("and the code as the job left it", ask(op="read", path="hello.py", at="work")["text"],
        '2:     return "Hi, " + name')
    check("the shared directory is empty again, and so is the worker's own",
          ([n for n in os.listdir(work.JOBS) if n != "hold"], os.listdir(work.JOB_DIR)), ([], [rec["id"]]))

    print("== the fence, tried from inside a job")
    home = os.path.expanduser("~")
    try:
        if home.startswith(("/tmp", tempfile.gettempdir())):
            raise OSError("home is under the temporary directory")
        outside = tempfile.mkdtemp(prefix=".douglas-work-test-", dir=home)
        with open(os.path.join(outside, "secret.txt"), "w") as f:
            f.write("the owner's own\n")
    except OSError as e:
        outside = ""
        print(f"  (no place outside the fence could be made, so two checks are left out: {e})")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    marker = f"douglas-work-test-marker-{os.getpid()}"
    plant = f"douglas-work-planted-{os.getpid()}"      # what an escape would leave in the real /tmp
    connect = (f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), 2); "
               "print('CONN' + 'ECTED')\"")
    rec = job("Try every way out.", [
        says("", ("run", {"command": f"cat {outside}/secret.txt; echo stolen > {outside}/planted.txt"})),
        says("", ("run", {"command": connect})),
        says("", ("run", {"command": "cat /etc/hosts; head -c 20 /etc/passwd; echo; kill -9 " + str(os.getpid())})),
        says("", ("run", {"command": "ln -s /etc/passwd link.txt; mkdir sub; ln -s / sub/up; "
                                     "ln -s ../../.. sub/out; echo made"})),
        says("", ("read", {"path": "link.txt"}), ("write", {"path": f"sub/up/tmp/{plant}-1.txt", "content": "x"}),
             ("read", {"path": "sub/out/job.json"}), ("read", {"path": "../../x"})),
        says("", ("read", {"path": ".git/config"}), ("write", {"path": f"/tmp/{plant}-2.txt", "content": "x"}),
             ("list", {"path": "sub/up"}), ("replace", {"path": "link.txt", "old": "root", "new": "toor"})),
        says("", ("run", {"command": f"(exec -a {marker} sleep 300 &) ; sleep 30", "seconds": 5})),
        says("", ("run", {"command": "echo inside > kept.txt; cat kept.txt; id -un > /dev/null; echo fine"})),
        says("", ("done", {"summary": "Tried."})),
    ])
    got = shown()
    if outside:
        has("a file of the owner's cannot be read, nor one written beside it", got[0], "Permission denied",
            never=["the owner's own"])
        check("and nothing was planted", os.path.exists(os.path.join(outside, "planted.txt")), False)
        shutil.rmtree(outside, ignore_errors=True)
    has("no connection goes out, even to this machine", got[1], "Permission", never=["CONNECTED"])
    conn_ok = subprocess.run(connect, shell=True, capture_output=True, text=True).stdout
    check("(the same connection from outside the fence does go through)", conn_ok.strip(), "CONNECTED")
    has("a part of /etc it has no business in is shut, the rest is not, and it can signal nobody outside",
        got[2], "/etc/hosts: Permission denied", "root:", "Operation not permitted")
    has("a link out of the tree is not followed by the file tools", got[4], "leads outside the repository")
    check("neither to read, nor to write through, nor by climbing",
          (got[5], got[6], got[7]),
          ("Not done: that path leads outside the repository.",
           "Not done: that path leads outside the repository.",
           "Not done: paths are given from the top of the repository, and stay inside it."))
    check("git's own files and paths from the root are refused, and a link is no way to list or change",
          (got[8], got[9], got[10], got[11]),
          ("Not done: git's own files are not for reading or changing by hand.",
           "Not done: paths are given from the top of the repository, and stay inside it.",
           "Not done: that path leads outside the repository.",
           "Not done: that path leads outside the repository."))
    check("nothing was planted through the links",
          [n for n in (f"/tmp/{plant}-1.txt", f"/tmp/{plant}-2.txt") if os.path.exists(n)], [])
    for n in (f"/tmp/{plant}-1.txt", f"/tmp/{plant}-2.txt"):
        if os.path.exists(n):
            os.unlink(n)
    has("a command that runs too long is stopped", got[12], "Stopped: it had not finished after 5 seconds")
    time.sleep(0.3)
    check("and what it left running is gone with it",
          subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.strip(), "")
    has("inside its own tree it works as usual", got[13], "inside", "fine", "Exit code 0")
    if not work.alone():
        out = subprocess.run([sys.executable, "-I", os.path.join(here, "douglas-work.py"), "fence", "--loopback",
                              root, "--", "/bin/true"], capture_output=True, text=True)
        has("a command is let onto the network only where the network is the job's own: asked for on a "
            "machine's shared one, nothing is started", (out.returncode, out.stderr), "126",
            "not the job's own, so a command gets none of it")
    check("this process is still here", (rec["state"], os.getpid() > 0), ("done", True))
    listener.close()

    print("== a job stands aside, stops, and runs out")
    with open(voice, "w") as f:
        json.dump({"state": "idle"}, f)
    MODEL["dropped"] = 0

    def talk(jid):
        for _ in range(100):                      # until the model is being asked
            if MODEL["seen"]:
                break
            time.sleep(0.05)
        with open(voice, "w") as f:
            json.dump({"state": "thinking"}, f)
        for _ in range(100):
            if MODEL["dropped"]:
                break
            time.sleep(0.05)
        TALK.update(held=os.path.exists(work.HOLD), status=ask(op="status")["text"])
        time.sleep(0.6)
        TALK["asked_while_held"] = len(MODEL["seen"])
        with open(voice, "w") as f:
            json.dump({"state": "idle"}, f)

    TALK = {}
    rec = job("Wait your turn.", [says("", ("done", {"summary": "Waited."}), delay=1.5)], during=talk)
    check("while someone talks to Douglas the model's answer is dropped and the job waits",
          (MODEL["dropped"], TALK["held"], TALK["asked_while_held"]), (1, True, 1))
    has("and says so if asked", TALK["status"], "Still at it", "paused while someone is talking")
    check("then it asks again, and the step is counted once",
          (rec["state"], rec["step"], len(MODEL["seen"]),
           MODEL["seen"][0]["messages"] == MODEL["seen"][1]["messages"]), ("done", 1, 2, True))
    check("the hold is lifted when the job is over", os.path.exists(work.HOLD), False)

    def stop(jid):
        for _ in range(100):
            if len(MODEL["seen"]) >= 2:
                break
            time.sleep(0.05)
        STOP["second"] = ask(op="work", repo="kettle", task="Another job at the same time.")["text"]
        STOP["said"] = ask(op="stop")["text"]

    STOP = {}
    rec = job("Write a note and then dawdle.", [
        says("", ("write", {"path": "notes/today.txt", "content": "half done\n"})),
        says("", ("done", {"summary": "never reached"}), delay=30)], during=stop)
    has("one job at a time", STOP["second"], "still under way", "One at a time")
    has("told to stop, it says what it keeps", STOP["said"], "told to stop", "keeps what it has changed")
    check("it stops, and what it had changed is kept on its branch",
          (rec["state"], rec["commits"], rec["files"], "told to stop" in rec["error"]),
          ("stopped", 1, ["notes/today.txt"], True))
    has("the report says it was not finished", work.Manager.report(rec),
        "was stopped before it was finished", "What went wrong: it was told to stop")

    work.STEPS = 4
    rec = job("Look around for ever.", [says("", ("list", {"path": ""})) for _ in range(9)])
    work.STEPS = 30
    check("a job that never finishes runs out of steps, and nothing was changed",
          (rec["state"], rec["step"], rec["commits"], rec["error"]),
          ("stopped", 4, 0, "it ran out of steps"))
    has("it was told when it was nearly out", str(MODEL["seen"][-1]["messages"]), "almost out of room")
    has("and the report says no file was changed", work.Manager.report(rec), "No file was changed")

    work.DISK_MB = 3
    rec = job("Fill the disk.", [
        says("", ("run", {"command": "head -c 6000000 /dev/zero > big.bin; echo made"})),
        says("", ("done", {"summary": "never reached"}))])
    work.DISK_MB = 4096
    check("a job that fills more of the disk than it may is stopped, and what it made is thrown away",
          (rec["state"], rec["commits"], rec["error"], os.path.exists(os.path.join(work.JOB_DIR, rec["id"]))),
          ("stopped", 0, "it filled more of the disk than a job may, and what it made was thrown away", False))

    rec = job("Say it instead of doing it.", [
        says("I think the code is fine as it is."), says("Really, nothing needs changing.")])
    check("a model that calls no tool is asked once, and then its words are its summary",
          (rec["state"], rec["step"], rec["summary"], rec["commits"]),
          ("done", 2, "Really, nothing needs changing.", 0))
    has("it was asked", MODEL["seen"][1]["messages"][-1]["content"], "You called no tool")

    rec = job("Trip over the tools.", [
        says("", ("read", '{"path": "hello.py"')),
        says("", ("fly", {"to": "the moon"}), ("read", {"path": "no/such/file.py"}),
             ("replace", {"path": "hello.py", "old": "zebra", "new": "horse"}),
             ("replace", {"path": "hello.py", "old": "e", "new": "E"}),
             ("write", {"path": "fifth.txt", "content": "one too many"})),
        says("", ("done", {"summary": "Tripped."}))])
    got = shown()
    check("arguments that are not JSON, a tool that is not there, a file that is not there",
          (got[0], got[1], got[2]),
          ("Not done: the arguments were not valid JSON. Call the tool again.",
           "There is no tool called 'fly'.", "Not done: No such file or directory."))
    has("text to replace that is not there, or is there many times", got[3] + got[4],
        "is in hello.py 0 times", "copy the text exactly", "times, and it has to be there exactly once",
        "Give more of the text around it")
    check("and a fifth tool in one step is put off", (got[5], rec["commits"]),
          ("Not done: four tools a step at most. Call it again.", 0))

    url, work.LLM_URL = work.LLM_URL, "http://127.0.0.1:9"
    rec = job("Think with a model server that is not there.", [])
    work.LLM_URL = url
    has("with the model server not answering, the job fails and says why",
        (rec["state"], rec["error"], rec["commits"]), "failed", "the model server", "0")

    print("== what the worker hands back is checked")

    def handed(make) -> dict:
        r = ask(op="work", repo="jobContextMCP", task="A result made up by hand.")
        d = os.path.join(work.JOBS, r["job"])
        make(d, m.jobs[r["job"]])
        for _ in range(60):
            if r["job"] not in m.jobs:
                break
            time.sleep(0.1)
        return next(x for x in m.records() if x["id"] == r["job"])

    def status_of(d, **more):
        with open(os.path.join(d, "status.json"), "w") as f:
            json.dump(dict({"state": "done", "step": 3, "updated": time.time()}, **more), f)

    elsewhere = os.path.join(root, "elsewhere.json")
    with open(elsewhere, "w") as f:
        json.dump({"state": "done", "summary": "read through a link"}, f)
    rec = handed(lambda d, j: os.symlink(elsewhere, os.path.join(d, "status.json")))
    check("a report that is a link to another file is not followed",
          (rec["state"], rec["summary"], rec["error"]),
          ("failed", "", "the worker's report could not be read"))

    def a_bundle(branch, onto=""):
        """A bundle made somewhere else: one commit on `branch`, on top of
        the repository's code or of nothing."""
        scratch = tempfile.mkdtemp(dir=root)
        if onto:
            sh("git", "clone", "-q", os.path.join(remotes, "acme", "jobContextMCP.git"), scratch)
            sh("git", "checkout", "-q", "-b", branch, onto, cwd=scratch)
        else:
            sh("git", "init", "-q", "-b", branch, scratch)
        put(scratch, {"planted.txt": "from somewhere else\n"})
        sh("git", "add", "-A", cwd=scratch)
        sh("git", "commit", "-q", "-m", "planted", cwd=scratch)
        out = os.path.join(scratch, "made.bundle")
        sh("git", "bundle", "create", out, (f"{onto}.." if onto else "") + f"refs/heads/{branch}", cwd=scratch)
        return out

    def link_result(d, j):
        os.symlink(a_bundle(j["branch"], j["base"]), os.path.join(d, "result.bundle"))
        status_of(d)

    rec = handed(link_result)
    check("a result that is a link is not followed either", (rec["state"], rec["commits"]), ("failed", 0))
    check("and no branch came of it", bare_git("rev-parse", "--verify", "--quiet",
                                               "refs/heads/" + rec["branch"]).strip(), "")

    def other_branch(d, j):
        shutil.copy(a_bundle("douglas/somebody-elses", j["base"]), os.path.join(d, "result.bundle"))
        status_of(d)

    rec = handed(other_branch)
    has("a result for a branch the job was not given is refused", rec["error"],
        "did not hold the branch the job was given")
    check("and that branch is not in the clone", bare_git(
        "rev-parse", "--verify", "--quiet", "refs/heads/douglas/somebody-elses").strip(), "")

    def other_history(d, j):
        shutil.copy(a_bundle(j["branch"]), os.path.join(d, "result.bundle"))
        status_of(d)

    rec = handed(other_history)
    has("a result not built on the code that was handed over is refused", rec["error"],
        "not built on the code the job was given")
    check("and its branch is taken away again", bare_git("rev-parse", "--verify", "--quiet",
                                                         "refs/heads/" + rec["branch"]).strip(), "")

    def good(d, j):
        shutil.copy(a_bundle(j["branch"], j["base"]), os.path.join(d, "result.bundle"))
        status_of(d, summary="x" * 5000, doing="y" * 500)

    rec = handed(good)
    check("a result that is what it should be is taken, and a long account is cut",
          (rec["state"], rec["commits"], rec["files"], len(rec["summary"]) <= 1504),
          ("done", 1, ["planted.txt"], True))
    rec = handed(lambda d, j: open(os.path.join(d, "status.json"), "w").write("[1, 2]"))
    check("a report that is not a report", (rec["state"], rec["error"]),
          ("failed", "the worker's report could not be read"))
    rec = handed(lambda d, j: status_of(d))
    has("done with nothing to show for it is said to be so", work.Manager.report(rec), "finished",
        "No file was changed")

    def odd_ask(d, j):
        with open(os.path.join(d, "ask.json"), "w") as f:
            json.dump({"n": 1, "messages": [{"role": "root", "content": "x"}], "tools": [{"type": "own"}]}, f)
        for _ in range(50):
            if os.path.exists(os.path.join(d, "answer.json")):
                break
            time.sleep(0.1)
        with open(os.path.join(d, "answer.json")) as f:
            ODD.update(json.load(f))
        status_of(d, state="failed")

    ODD, n = {}, len(MODEL["seen"])
    handed(odd_ask)
    has("a question that is no conversation is not passed on to the model", ODD.get("error", ""),
        "no conversation")
    check("and the model server never saw it", len(MODEL["seen"]) - n, 0)

    print("== what the journal holds")
    journal = "\n".join(LOG)
    has("which repository and how much, for each thing asked", journal, "cloned acme/jobContextMCP (public)",
        "fetched acme/jobContextMCP: 1 new commits", "search acme/jobContextMCP:", "job ", " done: 1 commits")
    check("and never the token, the words searched for, the job's words or the code",
          [w for w in ("not-a-real-token", "load_stories", "zebra", "greeting", "Hello", "story store",
                       "Try every way out") if w in journal], [])

shutil.rmtree(root, ignore_errors=True)
print("douglas-work tests passed" if not FAILS else f"{FAILS} FAILED")
sys.exit(1 if FAILS else 0)
