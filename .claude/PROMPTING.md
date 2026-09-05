# Prompting Rotunda work for fewer tokens

The big costs, in order: (1) how much of the repo I have to read to find the thing,
(2) how long the session has been running, (3) how many times I re-verify work.
Almost every trick below attacks #1 or #2.

## 1. Name the file. Always.

The single highest-leverage habit. Without a path I grep, guess, and read.

- Bad: "the flagged bills page shows the wrong date"
- Good: "in `templates/flagged.html` + the `/api/flagged` handler in `app.py`, the date shows UTC not Pacific"

If you don't know the file, say the nearest landmark you *do* know — a route
(`/clients`), a page constant (`REPORT_BODY`), a function (`snapshot_bill_state`),
an on-screen string ("No bills flagged yet"). Any of those is a one-grep answer.

## 2. Say what changes, not what's wrong

"Fix the client dropdown" makes me investigate. "In `static/js/client_quickadd.js`,
sort the client list alphabetically before rendering" makes me edit.

When you genuinely don't know the cause, say so explicitly — "don't know why, please
diagnose" — so I don't over-read a file you already know is fine.

## 3. `/clear` between unrelated tasks

Every message re-sends the whole conversation. A 3-hour session where you moved from
digest emails → CSS → FPPC forms is paying for the digest discussion on every CSS turn.
Clear when the topic changes. Keep going when it's the same thread of work.

## 4. Batch related asks into one prompt

Three small changes to the same file in one message costs roughly one file read.
Three separate messages cost three. Batch by *file*, not by theme.

- Good: "In `templates/report.html`: widen the bill column, drop the 'Status' header,
  and make row click open in a new tab."

## 5. Give the error, not the story

Paste the traceback, the failing pytest line, the browser console error. One pasted
stack trace replaces several rounds of me reproducing it.

Trim it though — the last 15 lines of a traceback and the failing assert are the whole
value; 400 lines of pytest collection output is not.

## 6. Set the scope ceiling out loud

Short phrases that reliably cut work:

- "just tell me, don't change anything"
- "one-line answer"
- "don't run the tests, I'll run them"
- "don't refactor anything else"
- "small fix, don't over-engineer"

Conversely, "look into X" is an open-ended budget. Use it deliberately.

## 7. Skip the pleasantries and the preamble

Not for politeness reasons — context you write is context I re-read every turn.
"Here's what I'm thinking, so a while back we talked about..." costs real tokens.
Lead with the ask.

## 8. Don't ask me to double-check work I just did

Edits fail loudly if they fail. "Can you verify the file looks right?" forces a re-read
of a file I already have. Ask for verification only when behavior is genuinely in
question (does the page render, does the test pass).

## 9. Repeat standing rules once, in CLAUDE.md — not in every prompt

`CLAUDE.md` is already loaded every session. If you find yourself saying "remember,
branch off main" a third time, that belongs in CLAUDE.md instead. Same for anything
about how this codebase works.

## 10. Prefer narrow questions over "explain X"

"How does the digest decide who gets email?" → I read `digest.py` and answer.
"Explain the digest system" → I read `digest.py`, `mailer.py`, `db.py`, `config.py`,
`render.yaml`, and the cron wiring.

---

## Templates

**Bug**
```
<file/route>. Symptom: <what you see>. Expected: <what you want>.
[paste error]
Fix it, don't touch anything else.
```

**Feature**
```
Add <thing> to <file/page>. It should <behavior>.
Follow the pattern already used by <similar existing thing>.
```

**Question**
```
Short answer only: <question about one specific file/function>?
```

## The Rotunda-specific one

`app.py` is still the expensive file — it's the one place where a vague prompt turns
into a huge read. When your ask touches it, include the route string or the page-body
constant name. That one detail is worth more than everything else on this page.
