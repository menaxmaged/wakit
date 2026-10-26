"""Open the wakit window: a page in your browser, served by this computer only (127.0.0.1).

Every command and option of the command line is there as a form; runs show their output live. Nothing is sent
anywhere - the page talks only to this local server, which runs the same `wakit` commands you would type.

example:
  wakit gui
  wakit gui --port 8765 --no-browser
"""
import argparse, importlib, json, os, secrets, string, subprocess, sys, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .cli import COMMANDS

PATHS = {"db", "export", "backup", "media", "out", "wa_db", "media_out", "old", "new", "src", "from_files",
         "files_out", "input", "like", "key"}
JOBS, LOCK = {}, threading.Lock()


def fields(parser):
    out = []
    for act in parser._actions:
        if isinstance(act, argparse._HelpAction) or isinstance(act, argparse._SubParsersAction):
            continue
        if not act.option_strings:
            continue
        kind = "bool" if isinstance(act, argparse._StoreTrueAction) else "choice" if act.choices else \
            "path" if act.dest in PATHS else "text"
        out.append({"dest": act.dest, "flag": max(act.option_strings, key=len), "help": act.help or "",
                    "required": bool(act.required), "kind": kind, "choices": list(act.choices or []),
                    "default": act.default if isinstance(act.default, (str, int, float)) and not
                    isinstance(act.default, bool) else None})
    return out


def schema():
    cmds = []
    for name, (mod, summary, section) in COMMANDS.items():
        if name == "gui":
            continue
        p = importlib.import_module(f".{mod}", __package__).build_parser()
        doc = (p.description or "").strip()
        subs = next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)
        entry = {"name": name, "summary": summary, "section": section, "doc": doc, "fields": fields(p)}
        if subs:
            helps = {c.dest: c.help for c in subs._choices_actions}
            entry["subcommands"] = [{"name": n, "summary": helps.get(n, ""), "fields": fields(sp)}
                                    for n, sp in subs.choices.items()]
        cmds.append(entry)
    return {"version": __version__, "commands": cmds, "home": os.path.expanduser("~")}


def build_argv(req):
    cmd = next((c for c in schema()["commands"] if c["name"] == req.get("cmd")), None)
    if not cmd:
        raise ValueError("unknown command")
    values = req.get("args") or {}
    argv = []
    if req.get("serial"):
        argv += ["--serial", req["serial"]]
    argv.append(cmd["name"])

    def opts(fs):
        out = []
        for f in fs:
            v = values.get(f["dest"])
            if f["kind"] == "bool":
                if v:
                    out.append(f["flag"])
            elif v not in (None, ""):
                out += [f["flag"], os.path.expanduser(str(v)) if f["kind"] == "path" else str(v)]
            elif f["required"]:
                raise ValueError(f"{f['flag']} is required")
        return out

    argv += opts(cmd["fields"])
    if "subcommands" in cmd:
        sub = next((s for s in cmd["subcommands"] if s["name"] == req.get("sub")), None)
        if not sub:
            raise ValueError("pick what to do")
        argv += [sub["name"]] + opts(sub["fields"])
    return argv


def child_env():
    """the child runs `python -m wakit` from the home folder: make this copy of wakit (in src/ or site-packages)
    importable there"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.environ.get("PYTHONPATH")
    return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": here + (os.pathsep + path if path else "")}


def run_job(argv):
    jid = secrets.token_hex(6)
    proc = subprocess.Popen([sys.executable, "-u", "-m", "wakit", *argv], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, bufsize=1,
                            cwd=os.path.expanduser("~"), env=child_env())
    job = {"argv": argv, "lines": [], "code": None, "proc": proc}
    with LOCK:
        JOBS[jid] = job

    def pump():
        for line in proc.stdout:
            job["lines"].append(line.rstrip("\n"))
        job["code"] = proc.wait()

    threading.Thread(target=pump, daemon=True).start()
    return jid


def listdir(path):
    path = os.path.abspath(os.path.expanduser(path or "~"))
    if not os.path.isdir(path):
        path = os.path.dirname(path)
    items = []
    try:
        for e in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower())):
            if not e.name.startswith("."):
                items.append({"name": e.name, "dir": e.is_dir()})
    except OSError as err:
        return {"path": path, "parent": os.path.dirname(path), "items": [], "error": str(err)}
    return {"path": path, "parent": os.path.dirname(path), "items": items}


class Handler(BaseHTTPRequestHandler):
    token = ""

    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        if self.headers.get("X-Wakit-Token") != self.token:
            self.send(403, {"error": "bad token - open the link wakit printed"})
            return False
        return True

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            page = open(os.path.join(os.path.dirname(__file__), "web", "index.html"), "rb").read()
            return self.send(200, page, "text/html; charset=utf-8")
        if not u.path.startswith("/api/") or not self.authed():
            return None if u.path.startswith("/api/") else self.send(404, {"error": "not found"})
        if u.path == "/api/schema":
            return self.send(200, schema())
        if u.path == "/api/ls":
            return self.send(200, listdir(q.get("path")))
        if u.path == "/api/job":
            job = JOBS.get(q.get("id", ""))
            if not job:
                return self.send(404, {"error": "no such job"})
            since = int(q.get("since", 0))
            return self.send(200, {"lines": job["lines"][since:], "next": len(job["lines"]), "code": job["code"]})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if not self.authed():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        if u.path == "/api/preview":
            try:
                return self.send(200, {"argv": build_argv(body)})
            except ValueError as e:
                return self.send(400, {"error": str(e)})
        if u.path == "/api/run":
            try:
                argv = build_argv(body)
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            return self.send(200, {"id": run_job(argv), "argv": argv})
        if u.path == "/api/stop":
            job = JOBS.get(body.get("id", ""))
            if job and job["code"] is None:
                job["proc"].terminate()
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=0, help="port to listen on (default: any free port)")
    p.add_argument("--no-browser", action="store_true", help="just print the link, do not open the browser")
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    Handler.token = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{srv.server_port}/#{Handler.token}"
    print(f"wakit is running at {url}\n(only this computer can open it; press Ctrl+C to stop)")
    if not a.no_browser:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        for job in JOBS.values():
            if job["code"] is None:
                job["proc"].terminate()


if __name__ == "__main__":
    main()
