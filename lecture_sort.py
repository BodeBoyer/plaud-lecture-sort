#!/usr/bin/env python3
"""Plaud lecture sorter: pull new Plaud recordings, classify by class, append
summary + transcript to one Google Doc per class per month (NotebookLM / Claude
Projects auto-sync Google Docs), plus a local markdown copy.

  python lecture_sort.py run [--dry-run] [--days 7]
  python lecture_sort.py self-test

One-time: npm i -g @plaud-ai/cli && plaud login; put Google OAuth desktop client
JSON at credentials.json (first run opens browser consent -> token.json).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "classes.json"
STATE = HERE / "state.json"
NOTES = HERE / "notes"
NEW_DOCS = HERE / "NEW_DOCS.txt"
LOG = HERE / "lecture_sort.log"
SCOPES = ["https://www.googleapis.com/auth/documents",
          "https://www.googleapis.com/auth/drive.file"]

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_ID_RE = re.compile(r"^(of_)?[0-9a-f]{32}$", re.I)  # Plaud added the of_ prefix Sept 2026
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------- plaud ----------
def plaud(*args: str) -> str:
    """Run plaud CLI, return stdout ('' on failure). Exit code 2 = not logged in."""
    import shutil
    exe = shutil.which("plaud")  # .cmd shim on Windows, so resolve instead of shell=True
    try:
        p = subprocess.run([exe or "plaud", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        sys.exit("`plaud` not found: npm install -g @plaud-ai/cli, then `plaud login`")
    if p.returncode == 2:
        sys.exit("plaud auth error: run `plaud login`")
    return p.stdout if p.returncode == 0 else ""


def parse_recent(text: str) -> list[dict]:
    """Rows: <32-hex id> <name...> <YYYY-MM-DD> <duration>. Name may hold spaces
    and even a timestamp; id=first, duration=last, date=second-to-last."""
    out = []
    for raw in text.splitlines():
        line = _ANSI.sub("", raw).strip()
        toks = line.split()
        if len(toks) < 3 or not _ID_RE.match(toks[0]) or not _DATE_RE.match(toks[-2]):
            continue
        out.append({"id": toks[0], "name": " ".join(toks[1:-2]),
                    "created_at": toks[-2], "duration": toks[-1]})
    return out


def duration_minutes(s: str) -> float:
    total = 0.0
    for num, unit in re.findall(r"(\d+)([hms])", s):
        total += int(num) * {"h": 60, "m": 1, "s": 1 / 60}[unit]
    return total


# ---------- classify ----------
def keyword_scores(text: str, classes: dict) -> dict[str, int]:
    low = text.lower()
    return {k: sum(len(re.findall(r"\b" + re.escape(kw.lower()) + r"\b", low))
                   for kw in c["keywords"]) for k, c in classes.items()}


def llm_key() -> str:
    """OPENROUTER_API_KEY from the environment, else from a `.env` file next to this script."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    env = HERE / ".env"
    if not key and env.exists():
        m = re.search(r"OPENROUTER_API_KEY\s*=\s*['\"]?([^'\"\s]+)", env.read_text(encoding="utf-8"))
        key = m.group(1) if m else ""
    return key


def llm_classify(text: str, cfg: dict) -> str:
    import requests
    base = cfg.get("llm_base_url", "https://openrouter.ai/api/v1").rstrip("/")
    key = llm_key() or ("ollama" if "openrouter" not in base else "")
    if not key:
        return "unknown"
    classes = cfg["classes"]
    system = ("Classify a college lecture transcript into exactly one class. Reply "
              "with ONLY the class key, nothing else, or `unknown` if none fit.\n" +
              "\n".join(f"- {k}: {c['name']} (topics: {', '.join(c['keywords'][:12])})"
                        for k, c in classes.items()))
    body = {"model": cfg["llm_model"], "temperature": 0, "max_tokens": 8,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": " ".join(text.split()[:3000])}]}
    if "openrouter" in base:
        body["provider"] = {"sort": "price"}
    try:
        r = requests.post(f"{base}/chat/completions", json=body,
                          headers={"Authorization": f"Bearer {key}"}, timeout=60)
        r.raise_for_status()
        ans = r.json()["choices"][0]["message"]["content"].strip().strip("`'\".").lower()
    except Exception as e:  # noqa: BLE001
        print(f"  llm error: {e}")
        return "unknown"
    return ans if ans in classes else "unknown"


def classify(text: str, cfg: dict, llm=llm_classify) -> tuple[str, str, dict]:
    """-> (class_key | 'unsorted', 'keyword' | 'llm', scores)."""
    scores = keyword_scores(text, cfg["classes"])
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, second = ranked[0], ranked[1]
    if top[1] >= 3 and top[1] >= 2 * second[1]:
        return top[0], "keyword", scores
    ans = llm(text, cfg)
    return (ans if ans != "unknown" else "unsorted"), "llm", scores


# ---------- google docs ----------
def google_services():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    tok, cred = HERE / "token.json", HERE / "credentials.json"
    creds = Credentials.from_authorized_user_file(tok, SCOPES) if tok.exists() else None
    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except Exception as e:  # noqa: BLE001  invalid_grant: expired (Testing-mode app) or revoked
                print(f"  google token refresh failed ({str(e)[:80]}); redoing consent")
        if not refreshed:
            if not cred.exists():
                sys.exit(f"missing {cred}: download OAuth desktop client JSON from Google Cloud")
            creds = InstalledAppFlow.from_client_secrets_file(cred, SCOPES).run_local_server(port=0)
        tok.write_text(creds.to_json())
        os.chmod(tok, 0o600)
    return build("drive", "v3", credentials=creds), build("docs", "v1", credentials=creds)


def drive_find_or_create(drive, name: str, mime: str, parent: str | None) -> tuple[str, bool]:
    safe = name.replace("\\", "\\\\").replace("'", "\\'")  # Drive query string escaping
    q = f"name = '{safe}' and mimeType = '{mime}' and trashed = false"
    if parent:
        q += f" and '{parent}' in parents"
    hits = drive.files().list(q=q, fields="files(id)").execute().get("files", [])
    if hits:
        return hits[0]["id"], False
    meta = {"name": name, "mimeType": mime}
    if parent:
        meta["parents"] = [parent]
    return drive.files().create(body=meta, fields="id").execute()["id"], True


def monthly_doc(drive, cfg: dict, state: dict, cls: str, month: str) -> str:
    key = f"{cls}/{month}"
    if key in state["docs"]:
        return state["docs"][key]
    folder_mime = "application/vnd.google-apps.folder"
    root, _ = drive_find_or_create(drive, cfg["drive_folder"], folder_mime, None)
    cname = cfg["classes"].get(cls, {}).get("name", "Unsorted")
    sub, _ = drive_find_or_create(drive, cname, folder_mime, root)
    doc_id, created = drive_find_or_create(drive, f"{cname} - {month}",
                                           "application/vnd.google-apps.document", sub)
    if created:
        url = f"https://docs.google.com/document/d/{doc_id}/edit"
        nb = cfg["classes"].get(cls, {}).get("notebook_id")
        if nb and notebooklm_add(nb, doc_id, f"{cname} - {month}"):
            print(f"  NEW DOC added to NotebookLM: {url}")
        else:
            with NEW_DOCS.open("a", encoding="utf-8") as f:
                f.write(f"{dt.date.today()}  {cname} - {month}  ADD TO NOTEBOOKLM: {url}\n")
            print(f"  NEW DOC -> add to NotebookLM: {url}")
    state["docs"][key] = doc_id
    return doc_id


# ---------- notebooklm (optional, unofficial notebooklm-py) ----------
def notebooklm_add(notebook_id: str, doc_id: str, title: str) -> bool:
    """Attach a Drive Doc to a NotebookLM notebook. False on any failure; the
    caller then falls back to NEW_DOCS.txt so a broken library never blocks."""
    try:
        import asyncio
        from notebooklm import NotebookLMClient

        async def go():
            async with NotebookLMClient.from_storage() as c:
                await c.sources.add_drive(notebook_id, doc_id, title)
        asyncio.run(go())
        return True
    except Exception as e:  # noqa: BLE001  ImportError, auth expired, RPC change
        print(f"  notebooklm add failed ({type(e).__name__}: {str(e)[:120]})")
        return False


def cmd_notebooks(_args) -> int:
    """Create one NotebookLM notebook per class (if missing), save ids to classes.json."""
    import asyncio
    from notebooklm import NotebookLMClient
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))

    async def go():
        async with NotebookLMClient.from_storage() as c:
            have = {n.title: n.id for n in await c.notebooks.list()}
            for key, cls in cfg["classes"].items():
                if cls.get("notebook_id"):
                    print(f"  {key}: {cls['notebook_id']} (kept)")
                    continue
                found = cls["name"] in have
                cls["notebook_id"] = have[cls["name"]] if found else (await c.notebooks.create(cls["name"])).id
                print(f"  {key}: {cls['notebook_id']} ({'found' if found else 'created'})")
    asyncio.run(go())
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"notebook ids saved to {CONFIG.name}")
    return 0


def docs_append(docs, doc_id: str, text: str) -> None:
    doc = docs.documents().get(documentId=doc_id, fields="body.content.endIndex").execute()
    end = doc["body"]["content"][-1]["endIndex"]
    docs.documents().batchUpdate(documentId=doc_id, body={"requests": [
        {"insertText": {"location": {"index": end - 1}, "text": text}}]}).execute()


# ---------- state ----------
def load_state(path: Path = STATE) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"seen": {}, "docs": {}}


def save_state(state: dict, path: Path | None = None) -> None:
    path = path or STATE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(path)


def entry_text(rec: dict, summary: str, transcript: str) -> str:
    d = dt.date.fromisoformat(rec["created_at"])
    head = f"{d} ({d.strftime('%a')}) - {rec['name']} - {rec['duration']}"
    return (f"\n\n{'=' * 20} {head} {'=' * 20}\nSUMMARY\n{summary.strip() or '(none)'}\n\n"
            f"TRANSCRIPT\n{transcript.strip()}\n")


# ---------- main flow ----------
def run(days: int, dry_run: bool, cfg: dict, state: dict, *, recent_text=None,
        fetch=None, llm=llm_classify, sink=None) -> list[tuple[str, str, str]]:
    """fetch(kind, id) -> text; sink(cls, month, text) -> doc_id. Injectable for tests."""
    recs = parse_recent(recent_text if recent_text is not None else plaud("recent", "-d", str(days)))
    fetch = fetch or (lambda kind, rid: plaud(kind, rid))
    if sink is None and not dry_run and not cfg.get("google_docs", True):
        sink = lambda cls, month, text: "local"  # noqa: E731  local markdown only
    if sink is None and not dry_run:
        drive, docs = google_services()

        def sink(cls, month, text):
            doc_id = monthly_doc(drive, cfg, state, cls, month)
            docs_append(docs, doc_id, text)
            return doc_id
    results = []
    for r in recs:
        rid = r["id"]                       # raw id, what the CLI wants
        key = rid.removeprefix("of_")       # state/file key, stable across the prefix change
        if key in state["seen"]:
            continue
        if duration_minutes(r["duration"]) < cfg["min_minutes"]:
            state["seen"][key] = {"class": "short", "at": dt.datetime.now().isoformat(timespec="seconds")}
            results.append((rid, "short", ""))
            continue
        transcript = (fetch("transcript", rid) or "").strip()
        if len(transcript.split()) < 50:
            results.append((rid, "pending", "no transcript yet"))
            continue
        summary = fetch("summary", rid) or ""
        cls, how, scores = classify(transcript, cfg, llm)
        month = r["created_at"][:7]
        text = entry_text(r, summary, transcript)
        local = NOTES / cls / f"{r['created_at']}_{key}.md"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(text.lstrip(), encoding="utf-8")
        line = f"{rid} {r['created_at']} {r['name'][:40]!r} -> {cls} ({how}, {scores})"
        if dry_run:
            print("  DRY", line)
            results.append((rid, cls, how))
            continue
        doc_id = sink(cls, month, text)
        state["seen"][key] = {"class": cls, "how": how, "doc": doc_id,
                              "at": dt.datetime.now().isoformat(timespec="seconds")}
        save_state(state)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} {line} doc={doc_id}\n")
        print("  ", line)
        results.append((rid, cls, how))
    if not dry_run:
        save_state(state)
    return results


def cmd_run(args) -> int:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    state = load_state()
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M} lecture_sort run (dry={args.dry_run})")
    res = run(args.days, args.dry_run, cfg, state)
    n = sum(1 for _, c, _ in res if c not in ("short", "pending"))
    print(f"{n} new lecture(s) sorted, {sum(1 for _, c, _ in res if c == 'pending')} pending transcription")
    return 0


# ---------- setup / status ----------
def cmd_status(_args) -> int:
    state = load_state()
    seen = [v for v in state["seen"].values() if v["class"] != "short"]
    per = {}
    for v in seen:
        per[v["class"]] = per.get(v["class"], 0) + 1
    last = LOG.read_text(encoding="utf-8").strip().splitlines()[-1] if LOG.exists() else "(never)"
    print(f"last log line: {last}")
    print(f"sorted lectures: {len(seen)}  by class: {per or '{}'}")
    print(f"monthly docs: {len(state['docs'])}  ->  {NEW_DOCS.name} lists ones to add to NotebookLM")
    return 0


def cmd_setup(_args) -> int:
    """Check every prerequisite, then register the logon task that starts the tray."""
    import shutil
    ok = True

    def check(label: str, good: bool, fix: str = ""):
        nonlocal ok
        ok &= good
        print(f"  [{'OK' if good else '!!'}] {label}" + ("" if good else f"  ->  {fix}"))

    check("Node.js", shutil.which("node") is not None, "install Node 20+ from nodejs.org")
    check("plaud CLI", shutil.which("plaud") is not None, "npm install -g @plaud-ai/cli")
    check("plaud logged in", (Path.home() / ".plaud" / "tokens.json").exists(), "plaud login")
    if not CONFIG.exists():
        import shutil as _sh
        _sh.copy(HERE / "classes.example.json", CONFIG)
        print(f"  [--] created {CONFIG.name} from classes.example.json; edit your classes + keywords")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if cfg.get("google_docs", True):
        check("credentials.json (Google OAuth desktop client)", (HERE / "credentials.json").exists(),
              "see README 'Google Docs' step")
        check("token.json (Google consent done)", (HERE / "token.json").exists(),
              "python lecture_sort.py run   (opens browser once)")
    else:
        print("  [--] google_docs is false: local markdown only")
    check("LLM key (OPENROUTER_API_KEY env var or .env file)",
          bool(llm_key()) or "openrouter" not in cfg.get("llm_base_url", "openrouter"),
          "set OPENROUTER_API_KEY, or point llm_base_url at a local Ollama")
    try:
        import pystray, PIL  # noqa: F401
        check("pystray + pillow", True)
    except ImportError:
        check("pystray + pillow", False, "pip install pystray pillow")
    try:
        from notebooklm.paths import get_storage_path
        nb_ok = Path(get_storage_path()).exists()
        print(f"  [{'OK' if nb_ok else '--'}] NotebookLM login (optional, auto-adds docs)"
              + ("" if nb_ok else "  ->  notebooklm login --browser chrome, then: python lecture_sort.py notebooks"))
    except ImportError:
        print("  [--] notebooklm-py not installed: NEW_DOCS.txt lists docs to add by hand")
    if sys.platform != "win32":
        print("  [--] task scheduler: Windows only; run tray.py at login via launchd/cron")
        return 0 if ok else 1
    q = lambda v: str(v).replace("'", "''")  # noqa: E731  PowerShell single-quote escape
    pyw, tray, here = q(Path(sys.executable).with_name("pythonw.exe")), q(HERE / "tray.py"), q(HERE)
    ps = (f"$a = New-ScheduledTaskAction -Execute '{pyw}' -Argument ('\"' + '{tray}' + '\"') -WorkingDirectory '{here}';"
          f"$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME;"
          f"$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
          f"-ExecutionTimeLimit (New-TimeSpan -Days 3650) -MultipleInstances IgnoreNew;"
          f"Register-ScheduledTask -TaskName LectureSort -Action $a -Trigger $t -Settings $s -Force | Out-Null")
    p = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    check("Task Scheduler 'LectureSort' (tray at logon)", p.returncode == 0, p.stderr.strip()[:200])
    print("setup complete" if ok else "fix the !! lines above, then rerun setup")
    return 0 if ok else 1


# ---------- self-test ----------
_FIXTURE = """Recordings in the last 7 days: 4
1ae0abdfd1367c40d81f39d05b005d10  2026-07-19 21:45:03  2026-07-20  8s
22fdc01d9165120576f91a97decb7202  Math lecture  2026-09-01  52m
e729230fc30670de7c2ec69c2fb8cf05  Steve Jobs & Bill Gates: A Conversation That Shaped Technology  2026-09-02  1h21m
a1487d9b1b31bac9fde725a524fff5a0  Untitled  2026-09-02  48m
of_69c78e38fd3259d662d8ac483c7c81a8  09-11 Lecture: Set Theory  2026-09-11  48m21s
"""
_T = {
    "22fdc01d9165120576f91a97decb7202": "Today we prove by induction that the theorem holds for every integer. "
                                        "The lemma uses a bijection and the pigeonhole principle. " * 3,
    "e729230fc30670de7c2ec69c2fb8cf05": "So we talked about stuff. Life is good. Nothing specific here at all, "
                                        "just a chat about the weather and lunch plans. " * 6,
    "a1487d9b1b31bac9fde725a524fff5a0": "hi",
}


def cmd_self_test(_args) -> int:
    import tempfile
    global NOTES, LOG, STATE, NEW_DOCS, notebooklm_add
    recs = parse_recent(_FIXTURE)
    assert [r["id"][:4] for r in recs] == ["1ae0", "22fd", "e729", "a148", "of_6"], recs
    assert recs[0]["name"] == "2026-07-19 21:45:03" and recs[2]["duration"] == "1h21m"
    assert abs(duration_minutes("1h21m") - 81) < 1e-9 and duration_minutes("8s") < 1

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cls, how, _ = classify(_T["22fdc01d9165120576f91a97decb7202"], cfg, llm=lambda *a: "fail")
    assert (cls, how) == ("math381", "keyword"), (cls, how)
    cls, how, _ = classify("The stack and the queue. " * 5, cfg, llm=lambda *a: "sportshist")
    assert (cls, how) == ("comp210", "keyword")  # 10 hits vs 0, no LLM
    cls, how, _ = classify(_T["e729230fc30670de7c2ec69c2fb8cf05"], cfg, llm=lambda *a: "finance")
    assert (cls, how) == ("finance", "llm")
    cls, _, _ = classify("nothing", cfg, llm=lambda *a: "unknown")
    assert cls == "unsorted"

    with tempfile.TemporaryDirectory() as d:
        NOTES, LOG, STATE = Path(d) / "notes", Path(d) / "log", Path(d) / "state.json"
        NEW_DOCS = Path(d) / "new_docs.txt"
        state = {"seen": {"69c78e38fd3259d662d8ac483c7c81a8": {"class": "math381"}}, "docs": {}}  # pre-prefix key
        sunk = []
        sink = lambda c, m, t: sunk.append((c, m)) or f"doc-{c}"  # noqa: E731
        fetch = lambda kind, rid: _T.get(rid, "") if kind == "transcript" else "SUM"  # noqa: E731
        run_ = lambda: run(7, False, cfg, state, recent_text=_FIXTURE, fetch=fetch,  # noqa: E731
                           llm=lambda *a: "finance", sink=sink)
        res = dict((rid, c) for rid, c, _ in run_())
        assert res["1ae0abdfd1367c40d81f39d05b005d10"] == "short"
        assert res["22fdc01d9165120576f91a97decb7202"] == "math381"
        assert res["e729230fc30670de7c2ec69c2fb8cf05"] == "finance"
        assert res["a1487d9b1b31bac9fde725a524fff5a0"] == "pending"
        assert "of_69c78e38fd3259d662d8ac483c7c81a8" not in res, "old-style state key must dedup of_ id"
        assert sunk == [("math381", "2026-09"), ("finance", "2026-09")], sunk
        assert state["seen"]["22fdc01d9165120576f91a97decb7202"]["doc"] == "doc-math381"
        md = (NOTES / "math381" / "2026-09-01_22fdc01d9165120576f91a97decb7202.md").read_text(encoding="utf-8")
        assert md.startswith("=") and "SUMMARY\nSUM" in md and "TRANSCRIPT" in md
        # rerun: nothing re-sunk, pending one retried and still pending
        assert [c for _, c, _ in run_()] == ["pending"] and len(sunk) == 2
        assert entry_text(recs[1], "", "x").count("(none)") == 1

        # NotebookLM auto-add: success skips NEW_DOCS, failure falls back to it
        class FakeDrive:
            def files(self): return self
            def list(self, **k): self.r = {"files": []}; return self
            def create(self, **k): self.r = {"id": "newdoc"}; return self
            def execute(self): return self.r
        cfg["classes"]["math381"]["notebook_id"] = "nb1"
        calls = []
        notebooklm_add = lambda nb, doc, title: calls.append((nb, doc, title)) or True  # noqa: E731
        assert monthly_doc(FakeDrive(), cfg, {"seen": {}, "docs": {}}, "math381", "2026-10") == "newdoc"
        assert calls == [("nb1", "newdoc", "MATH 381 Discrete Math - 2026-10")] and not NEW_DOCS.exists()
        notebooklm_add = lambda *a: False  # noqa: E731
        monthly_doc(FakeDrive(), cfg, {"seen": {}, "docs": {}}, "math381", "2026-10")
        assert "ADD TO NOTEBOOKLM" in NEW_DOCS.read_text(encoding="utf-8")
    print("self-test passed")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_run)
    sub.add_parser("self-test").set_defaults(func=cmd_self_test)
    sub.add_parser("setup", help="check prerequisites, register logon task").set_defaults(func=cmd_setup)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("notebooks", help="create a NotebookLM notebook per class, save ids").set_defaults(func=cmd_notebooks)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
