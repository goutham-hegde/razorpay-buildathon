# Recording runsheet

`video-script.md` is what to *say*. This is what to have on screen while saying it, as a list
of URLs and commands rather than a list of clicks — because the one shot guaranteed to be
fumbled on camera is "now let me find case B00106 in this six-hundred-case stream".

Every link below opens the console directly on the shot. Read the script; follow this.

---

## Before the take

Four commands, in this order. The first three take about a minute; the fourth stays running.

```bash
.venv/Scripts/python -m pytest                                    # expect 310 passed
.venv/Scripts/python -m reclaim.eval.replay --batch B --arms all --fresh
.venv/Scripts/python -m reclaim.core.guards --batch B             # expect 6/6, both arms
.venv/Scripts/python -m uvicorn reclaim.api.main:app --port 8000
```

If `replay` refuses with a `runs.run_id` collision, that is the ledger declining to overwrite
an audit trail, not a bug — `--fresh` is what you forgot.

Then open two browser windows and leave them on these:

| Window | URL |
|---|---|
| **A — live** | <http://127.0.0.1:8000/?batch=B&run=naive&case=case_B00072&zoom=present> |
| **B — results** | <http://127.0.0.1:8000/?batch=B&view=results&zoom=present> |

And one terminal, cleared, sitting in the repo root.

Console URLs take `batch`, `run` (arm name or run id), `case`, `view` (`live` or `results`)
and `zoom`. The address bar rewrites itself as you click, so any shot you find by hand is a
link you can paste back into this file.

**Record at 1920x1080 or wider, and leave the console in `Present`.** Every link here carries
`zoom=present`, which scales the whole interface — type, padding and controls together — to
something legible when a 1080p capture is played back in a browser tab. `Normal` is the desk
size and is too small on camera; it is what the first version of this console only had. The
setting sticks across reloads, so you set it once.

Below about 1800px wide in `Present`, the live view stacks into one column and puts the case
panel above the feed. That is deliberate and it still records fine — but two columns is the
better shot, so give the window the full width.

---

## The shots, in script order

### 0:00 — 0:33 · The trap

**Window A**, already loaded: `case_B00072` under the **naive** arm.

This is the whole cold open in one case. A Rs 9,999 recurring card mandate fails at
`payment_response` — *"timeout after debit instruction sent to AXIS"*. Naive retries it. The
trail's second row reads **DOUBLE CHARGE** in red.

Scroll rate: none. Let the trail sit still while you talk. The words "and the retry
**succeeds**" should land while that red row is on screen.

Then, without saying anything about it yet, switch the arm picker to **agent** — same case,
one `escalate` decision, closed `reconcile_hold`, no charge at all. That silent cut is worth
more than a sentence.

> <http://127.0.0.1:8000/?batch=B&run=agent&case=case_B00072&zoom=present>

### 0:33 — 1:19 · What it does

**Editor**, not the browser: `reclaim/core/policy.py`, the `WHAT EACH CAUSE BUYS` block in
the module docstring (near the top). Nine causes, nine actions, one screen, no scrolling.

Then scroll once to `_post_authorization_veto` if you want a second beat — but the script
does not need it here, and the same code has its own moment at 3:27.

### 1:19 — 2:19 · Why you can believe the number

**Terminal**, one command, let it run on camera. It takes about a second:

```bash
.venv/Scripts/python -m pytest tests/test_seal.py -v
```

Six green lines, and the name of the test is the argument:
`test_the_policy_reads_nothing_from_the_simulated_world`.

Then **Window A**, arm picker to **control**:

> <http://127.0.0.1:8000/?batch=B&run=control&zoom=present>

The stream says *"no actions recorded — which for the control arm is the entire point."* That
is the shot for "a control arm that does **nothing at all**", and it is better than any
diagram of one.

### 2:19 — 3:27 · The result, and the thing it caught

**Window B**. The four-arm table, whole, no scrolling.

The two columns to point at are `halt %` and `net Rs` — naive at **66.1%** and
**−5,188,982** against a gross figure that looks like a win. If you can highlight, highlight
the naive row's `net Rs` cell while saying "sixty lakh of future revenue".

For the sensitivity paragraph there is nothing to show and that is fine — stay on the table.
Do not cut to a terminal running `sensitivity`; it takes minutes and the silence will cost
you the take. The numbers are in `README.md` if a still is wanted instead.

### 3:27 — 4:37 · Bounded, and provable

**Window B**, scroll down to the invariants panel. Six green ticks per arm, and both asserted
arms reading `6/6 held`. Naive is on the same panel showing `5/6` with its twenty-six
double-charged cases listed underneath in red — that contrast is worth a beat, because it is
the same six checks run against the strawman.

Then **Window A**, and this is the payoff shot of the whole video:

> <http://127.0.0.1:8000/?batch=B&run=agent&case=case_B00106&zoom=present>

One decision. The reason field reads, in full:

> the opening failure was reported at payment_response, after the debit instruction had
> already been sent — the money may have moved and nothing observable says whether it did;
> holding for reconciliation instead of the retry this case was otherwise due

Let that be legible. It is the case that broke R1 on the first held-out run, it is the reason
there is a fourth gate, and a reviewer who reads that sentence off the screen has understood
the project.

If you want the before-and-after, `git show 5509291 --stat` is the D7 commit and `a802135`
is the fix — but the script does not call for it and the take is already at 5:15.

### 4:37 — 5:15 · What it does not do

**Browser**, the GitHub repo page, then the README results table. Nothing to operate.

---

## If something looks wrong

- **Console shows "No ledger for batch B"** — `replay` has not been run, or was run against a
  different `--root`. Re-run the setup block.
- **The arm picker has fewer than four arms** — `replay --arms all` was not used.
- **A number on screen disagrees with the script** — the script is what is wrong. Every figure
  in it came from the runs logged in `progress.md` under D8; re-run `metrics --batch B` and
  fix the script, never the other way round.
- **Invariants panel is not 6/6** — stop and find out why before recording. That table is the
  claim.

---

## Two things worth resisting

**A feature tour.** The strongest thing here is the control arm, and it is the one thing no
other submission will have. Time spent on the console's UI is time not spent on it.

**Re-recording to fix a stumble in the middle.** The spine is "why you can believe the
number", at 1:19 — 2:19. If that section is clean, a stumble elsewhere is survivable. If it
is not, the take is not worth keeping however good the rest was.
