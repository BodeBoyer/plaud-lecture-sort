# plaud-lecture-sort

Record every lecture on a [Plaud](https://www.plaud.ai) device. This tool pulls the
transcripts, figures out which class each one belongs to, and appends the AI
summary plus the full transcript to one Google Doc per class per month. Google
NotebookLM and Claude Projects both auto-sync Google Docs, so after you add a Doc
once, every later lecture shows up in your notebook by itself.

Sits in the system tray, syncs every 15 minutes, and pops a toast when a lecture
is sorted. Costs about $0.05 per semester in LLM calls.

```
Plaud device ──> Plaud app (auto transcribe + summary)
                      │  official `plaud` CLI
                      ▼
            lecture_sort.py  ── keyword match, LLM on ties ──> which class?
                      │
          ┌───────────┴───────────┐
          ▼                       ▼
 notes/<class>/*.md      Google Doc "<class> - 2026-09"
 (local, for Obsidian    (NotebookLM / Claude Projects
  or Claude Code)          sync it automatically)
```

## How classification works

1. Count keyword hits per class from `classes.json` (whole word, case-insensitive).
2. Clear winner (3+ hits and double the runner-up) wins outright. No LLM call.
3. Otherwise one cheap LLM call decides. Default model is
   `qwen/qwen3-30b-a3b-instruct-2507` on OpenRouter (~$0.0002 per lecture), picked
   as the cheapest model that scores well on instruction following. Point
   `llm_base_url` at Ollama (`http://localhost:11434/v1`) for a free local model.
4. If neither can tell, the recording goes to `Unsorted`. Non-lecture recordings
   (calls, memos) land there too instead of polluting a class notebook.

## Setup

Requirements: Python 3.11+, Node 20+, a Plaud account, Windows (tray + scheduler;
the core script runs anywhere).

```bash
git clone https://github.com/BodeBoyer/plaud-lecture-sort
cd plaud-lecture-sort
pip install -r requirements.txt
npm install -g @plaud-ai/cli
plaud login
cp classes.example.json classes.json     # edit: your classes + keywords
cp .env.example .env                     # put your OpenRouter key in it
python lecture_sort.py setup
```

`setup` prints a checklist and registers a Windows Task Scheduler task that starts
the tray app at logon.

### Google Docs (optional, needed for NotebookLM / Claude sync)

One-time, about 10 minutes:

1. [console.cloud.google.com](https://console.cloud.google.com): create a project.
2. APIs & Services, Library: enable **Google Docs API** and **Google Drive API**.
3. APIs & Services, OAuth consent screen: External. Either add yourself as a test
   user, or click **Publish app** (recommended; test-mode tokens expire weekly).
4. Credentials, Create credentials, OAuth client ID, **Desktop app**. Download the
   JSON and save it as `credentials.json` in this folder.
5. `python lecture_sort.py run` opens a browser consent once and saves `token.json`.

Don't want Google? Set `"google_docs": false` in `classes.json`. You get the local
markdown folder only.

### NotebookLM

One notebook per class. The first lecture of each month creates a new Doc and
appends its link to `NEW_DOCS.txt` (tray menu: "New docs to add to NotebookLM").
Open the notebook, Add source, Google Drive, pick that Doc. Done for the month.

## Commands

| | |
|---|---|
| `python lecture_sort.py run` | sync now |
| `python lecture_sort.py run --dry-run` | show classification, write nothing to Docs |
| `python lecture_sort.py status` | last run, counts per class |
| `python lecture_sort.py setup` | prerequisite checklist + register logon task |
| `python lecture_sort.py self-test` | offline tests |
| `pythonw tray.py` | tray app (setup registers this at logon) |

## Files

| | |
|---|---|
| `classes.json` | your classes, keywords, model, options (gitignored) |
| `state.json` | recordings already handled + Doc ids. Delete an id to re-ingest. |
| `notes/<class>/` | local markdown copy of every lecture |
| `NEW_DOCS.txt` | Docs that still need adding to NotebookLM |
| `lecture_sort.log` | one line per sorted lecture |
| `credentials.json`, `token.json`, `.env` | secrets, gitignored |

## Notes

- Plaud has no public API or webhooks. The official `@plaud-ai/cli` is free for all
  accounts and is the only zero-touch way in. Recordings shorter than
  `min_minutes` (default 5) are ignored.
- One Doc per class per month keeps each source well under NotebookLM's 500k-word
  cap.
- The tray uses ~100 MB RAM and negligible CPU between syncs.

MIT license.
