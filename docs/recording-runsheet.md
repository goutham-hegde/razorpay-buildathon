# Recording runsheet

`video-script.md` is what to *say*. This is what to have on screen while saying it, as a list
of URLs and commands rather than a list of clicks — because the one shot guaranteed to be
fumbled on camera is "now let me find case B00106 in this six-hundred-case stream".

Every link below opens the console directly on the shot. The script carries the same links
inline, so it can be read on its own; this file is the longer form — the setup block, the
framing for each shot, and what to do when something looks wrong. If the two ever disagree
about a timestamp, `python docs/wordcount.py` settles it.

---

## Before the take

Four commands, in this order. The first three take about a minute; the fourth stays running.

```bash
.venv/Scripts/python -m pytest                                    # expect 311 passed
.venv/Scripts/python -m reclaim.eval.replay --batch B --arms all --fresh
.venv/Scripts/python -m reclaim.core.guards --batch B             # expect 6/6, both arms
.venv/Scripts/python -m uvicorn reclaim.api.main:app --port 8000
```

If `replay` refuses with a `runs.run_id` collision, that is the ledger declining to overwrite
an audit trail, not a bug — `--fresh` is what you forgot.

Then open two browser windows and leave them on these:

| Window | URL |
|---|---|
| **A — case book** | <http://127.0.0.1:8000/?batch=B&run=naive&case=case_B00072&zoom=present> |
| **B — statement** | <http://127.0.0.1:8000/?batch=B&view=statement&zoom=present> |

And one terminal, cleared, sitting in the repo root.

Console URLs take `batch`, `run` (arm name or run id), `case`, `view` (`statement`, `book`
or `assurance`) and `zoom`. A link naming a `case` opens the case book on it. With no `batch`
the console opens on **B**, the reported one. The address bar rewrites itself as you click,
so any shot you find by hand is a link you can paste back into this file.

**Record at 1920x1080 or wider, leave the console on `Large`, and press F11.** Every link
here carries `zoom=present`, which scales the whole page — type, padding and controls
together — to something legible when a 1080p capture is played back in a browser tab.
`Normal` is the desk size and is too small on camera. The setting sticks across reloads, so
you set it once.

F11 is not optional. The browser's own chrome costs about 130px, and two shots need the
bottom of a 1080-tall viewport: the case panel's trail, and the fourth `Assurance` card.

**Which shots need a scroll, measured at 1920x1080 rather than guessed:**

| Shot | Scroll |
|---|---|
| Title, Close | none — one card, one viewport |
| Any `case=` link | **~4 wheel notches**, until `case_B00072` (or `_B00106`) sits near the top. The whole trail is then on screen, down to `closed`. Scroll *before* recording, then leave it. |
| `run=control&view=book` | **none.** It fits exactly, which is the joke — the arm that did nothing needs no room |
| `view=statement` | none for the claim and the derivation; **~8 notches** to bring `The same statement, four ways` to the top for the arm table |
| `view=assurance` | none for the three columns; **~1 notch** at the end, to bring up `R1 failed on 26 cases` and the `agent 6 of 6 held` card, which sit on a second row |

Below about 62rem wide, the live view stacks into one column and puts the case
panel above the feed. That is deliberate and it still records fine — but two columns is the
better shot, so give the window the full width.

---

## The shots, in script order

### 0:00 — 0:17 · Title

**A third window**, or the same one on another tab: `docs/card-open.html`, opened from the
filesystem. Fullscreen, nothing moves, nothing to operate. Hold two seconds before the first
word and two after the last, so the cut into the console has somewhere to land.

Check the name on it before recording — it is taken from the repository's git identity, not
from anything you typed.

The last line of the intro plants *"retries everything three times"*, and the last line of
the next section pays it off with *"and charges it twice"*. They are a pair; if one gets
rewritten, rewrite both or the payoff lands on nothing.

### 0:17 — 0:53 · The trap

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

### 0:53 — 1:30 · What it does

**The terminal** — the same window as the next section, which is one less application on
camera. `reclaim/core/policy.py` lines 32-51, the `WHAT EACH CAUSE BUYS` block:

```bash
sed -n '32,51p' reclaim/core/policy.py                                   # bash
Get-Content reclaim/core/policy.py | Select-Object -Skip 31 -First 20    # PowerShell
```

Twenty lines, 91 characters at the widest, which is comfortable at a 24px terminal font on a
1080 capture. Run it before the take so the output is already sitting there.

**Not an editor, and not GitHub**, both of which were tried. The block is inside a docstring,
so every editor colours the whole thing as one string — there is no highlighting to gain, and
an editor adds a sidebar, tabs and a minimap to pay for it. GitHub's blob view renders dark
by default, spends 320px on the file tree, and its `#L32-L51` anchor leaves the block at the
bottom edge of the viewport.

Land the last line — "anything that moves money is plain code behind a gate" — on
`SEALED. Imports nothing from the simulated world.`, which is the last line on screen.
`ambiguous_debited  never charge` is also sitting in that table, four rows up, tying back to
the case from the cold open without you having to say so.

Then scroll once to `_post_authorization_veto` if you want a second beat — but the script
does not need it here, and the same code has its own moment at 3:24.

### 1:30 — 2:32 · Why you can believe the number

**The terminal again**, same window and same size as the previous section. `clear` first, so
the output starts at the top of an empty screen rather than under the docstring.

One command, run on camera. It finishes in 0.16s, so there is no waiting shot — which is
why the *timing of the keystroke* is the direction here:

```bash
.venv/Scripts/python -m pytest tests/test_seal.py -vv
```

Have it typed at the prompt but **not executed** before you start recording. Say the first
two sentences over the bare command, and press Enter on the words *"and a test fails if it
does"* — the six lines land exactly there. The last sentence, "the most important test in the
repository", goes over the finished output. Typing it live only buys a fumble.

**Keep the terminal near 100 columns.** pytest sizes this output to the terminal, so a
maximized 1920px window at a small font gives ~130 columns and strands `PASSED` and `[ 16%]`
out at the right edge, far from the names that are the entire point. At ~100 columns — about
a 26px font at 1080 — the columns stay tight. That width also fits the previous section's
91-character block, so one terminal setting serves both.

**`-vv`, not `-v`.** `pyproject.toml` sets `addopts = "-q"`, and a single `-v` only cancels
it back to the default — you get six dots and no names, on the one shot whose whole point is
the names. With `-vv`:

```
tests/test_seal.py::test_core_has_no_static_import_of_synth        PASSED
tests/test_seal.py::test_core_never_references_synth_by_string     PASSED
tests/test_seal.py::test_the_poison_actually_bites                 PASSED
tests/test_seal.py::test_core_imports_cleanly_with_synth_poisoned  PASSED
tests/test_seal.py::test_core_cannot_read_ground_truth             PASSED
tests/test_seal.py::test_truth_is_a_separate_file_from_cases       PASSED
```

The one to let land is `test_core_cannot_read_ground_truth`, fifth of the six.

Leave pytest's header on screen. `platform win32 -- Python 3.13.13, pytest-9.1.1`,
`configfile: pyproject.toml` and `collected 6 items` are six lines of provenance for free,
and sixteen lines total still sits comfortably in a 1080 frame at 26px.

Then **Window A**, arm picker to **control**:

> <http://127.0.0.1:8000/?batch=B&run=control&view=book&zoom=present>

**This shot needs no scrolling.** It is the only one that fits a 1080 frame exactly, which is
the joke: the arm that did nothing needs no room. The stream reads *"No actions recorded.
This arm did nothing at all — which for the control arm is the entire point, and is what
every other arm's recovery figure is measured against."* Better than any diagram of one.

**The counter row is a trap, and the script is worded around it.** It reads `0 cases worked`,
`0 charges presented`, `0 messages sent`, **`0 payments recovered`** — and those are *replay*
counters, how much of the recorded stream has been played back, not what the arm achieved.
Saying "23.8% come back on their own" over a screen that says `0 payments recovered` reads as
a flat contradiction to anyone watching, in the one section whose whole subject is honesty.
So the line is *"a control arm that does nothing at all: no charges, no messages, nothing.
**And it still recovers 23.8%**"* — actions first, matching the zeros on screen, then the
recovery. Do not shorten it back.

**Do not try to show a control-arm Statement.** The arm picker drives the case book only;
`renderStatement` fixes its subject as `by.agent || by.rules || …`, so the Statement tab
reads "the **agent** arm recovered 58.2%" no matter what the picker says. That is deliberate
— there is one statement, not four — but it means switching tabs here would put the agent's
headline on screen under a sentence about the control arm.

### 2:32 — 3:24 · The result, and the thing it caught

**Window B**, the Statement tab, **scrolled about two notches** — far enough that the
`reclaim` masthead is gone and `THE CLAIM` is at the top of the frame. `.tabs` is
`position: sticky`, so the tab bar and the `BATCH B` / `ARM agent` pickers stay pinned while
the masthead goes.

That scroll is not cosmetic. Unscrolled at 1080 the derivation runs off the bottom at
`less  Recovered without any help — what the control arm collected  (4,20,657)`, so the
double-ruled total it is all building to — `Net lift over doing nothing  5,74,947` — is
below the fold. Reading a viewer down a sum whose answer is off screen is worse than not
showing the sum. Scrolled, the claim, the four figures and the complete derivation are in
one frame together.

Let a viewer read down it once before you say anything; it is the shot that makes the number
checkable rather than assertable. **Rs 2,47,129**, the figure the script names here, is in the
Notes column on the right under "is what the model is worth" — point at it rather than at the
arms table, which is not on screen yet.

Then scroll to the four-arm table.

**Press `End`, not a counted number of wheel notches.** The working table is the last thing
in the statement view, so the bottom of the page is a fixed, repeatable frame that holds
*both* tables at once. That is not a nicety: the four figures this section names are split
across them.

| Said | Shown | Where |
|---|---|---|
| "destroys **66.1%** of the recurring book" | `244 · 66.1%` | upper table, `MANDATES HALTED` |
| "**eight lakh rupees** of invoices" | `7,91,834` | **lower** table, `GROSS` |
| "**sixty lakh** of future revenue" | `(59,73,804)` | **lower** table, `FORFEITED` |
| "**Rs 62 lakh** behind the agent" | `(56,09,640)` against `5,74,947` | upper table, `LIFT ON CONTROL`, two rows apart |

So the choreography is upper table, down to the lower one, back up. Naive's `44.3%` sits two
columns from its `(51,88,983)` — that pairing, a recovery rate that reads as a win beside a
net that is a catastrophe, is the whole argument, and both are on the same row.

For the sensitivity paragraph there is nothing to show and that is fine — stay on the table.
Do not cut to a terminal running `sensitivity`; it takes minutes and the silence will cost
you the take. The numbers are in `README.md` if a still is wanted instead.

### 3:24 — 4:26 · Bounded, and provable

**Window B**, the **Assurance** tab:

> <http://127.0.0.1:8000/?batch=B&view=assurance&zoom=present>

**Unscrolled for the whole narration.** Measured at 1920x1080 with `zoom=present`, the frame
holds the three columns entire — headers at content-y 444, R1 at 546, R6 at 976, the closing
rule at 1001 — so `control 6 of 6`, `naive 5 of 6` and `rules 6 of 6` read across one line
with naive's `R1 FAILED` in red below them. That contrast is the shot: the same six checks
run against the strawman.

**The fourth card is not missing.** It is a three-column grid, so `agent` wraps to a second
row and starts at content-y 1392 — below the fold, and 990px from the three headers, which is
just wider than a 1080 frame minus the sticky tab bar. The two cannot be in one frame, so do
not try.

**One scroll, and it is the last thing you do.** On "a constraint has no window", scroll until
the arm names tuck under the tab bar. That lands the pink `R1 failed on 26 cases` list
(content-y 1021-1359, with `case_B00072` at the top of it — the case from the cold open) and
the `agent 6 of 6 held` header at 1434. It is the only frame in the entire cut that shows the
arm the video is about holding all six, so it is worth the move.

Then **Window A**, and this is the payoff shot of the whole video:

> <http://127.0.0.1:8000/?batch=B&run=agent&case=case_B00106&zoom=present>

One decision. The reason field reads, in full:

> the opening failure was reported at payment_response, after the debit instruction had
> already been sent — the money may have moved and nothing observable says whether it did;
> holding for reconciliation instead of the retry this case was otherwise due

Let that be legible. It is the case that broke R1 on the first held-out run, it is the reason
there is a fourth gate, and a reviewer who reads that sentence off the screen has understood
the project.

**Scroll as in the cold open** — about four notches, until `case_B00106` tucks under the tab
bar. The panel runs roughly 1,040px from the title to the last trail row against about
1,005px of usable frame once the sticky tab bar takes its 75, so the final `closed ·
reconcile_hold` row lands on or just past the bottom edge. Leave it there; `close · case
closed as reconcile_hold` is directly above and carries the same fact.

**The detail to point at is `Bank reference: none returned`,** in the error box, while saying
"confidently". The double-charge gate weighs the diagnoser's confidence *and* looks for a bank
reference, and this failure carried neither a low confidence nor a reference — which is the
whole reason the rule that replaced it consults no diagnosis at all. The contrast is exact
and both halves are in this cut:

| | `case_B00072` (cold open) | `case_B00106` (here) |
|---|---|---|
| rail | `card_recurring · AXIS` | `upi_autopay · BOB` |
| bank reference | `AXIS821756247497` | **none returned** |
| step | `payment_response` | `payment_response` |
| agent's action | `escalate` | `hold` |

Two different rails, two different reference situations, one shared `step` — and the veto
holds both, because `step` is the only thing it reads. Say "three entries, and not one of
them is a charge" if you want the count on screen to do work; the panel header reads
`EVERY ACTION TAKEN — 3 ENTRIES`.

If you want the before-and-after, `git show 5509291 --stat` is the D7 commit and `a802135`
is the fix — but the script does not call for it, and the spoken script already fills 5:00.

### 4:26 — 4:55 · What it does not do

**Browser**, the GitHub repo page, then the README results table. Nothing to operate.

### 4:55 — 5:00 · Close

`docs/card-close.html`, fullscreen. Three figures and the repo URL. Hold four seconds after
the last word — this is the frame anyone who wants to look the project up will pause on.

Those three figures are the reported batch B numbers, typed into the card by hand. They are
the only figures in this project that are not read back from the ledger, so if the ledger is
ever regenerated, re-read them with `metrics --batch B` and `guards --batch B` and edit the
card. The comment at the top of the file says so too.

---

## If something looks wrong

- **Console shows "No ledger for batch B"** — `replay` has not been run, or was run against a
  different `--root`. Re-run the setup block.
- **The arm picker has fewer than four arms** — `replay --arms all` was not used.
- **A number on screen disagrees with the script** — the script is what is wrong. Every figure
  in it was last re-derived from a clean clone under D10 in `progress.md`; re-run
  `metrics --batch B` and fix the script, never the other way round.
- **Invariants panel is not 6/6** — stop and find out why before recording. That table is the
  claim.

---

## Do not press Play

Not in any shot. Every case in this cut is reached by URL, and the transport controls exist
for someone browsing a batch, not for the recording.

On the **control** arm it is worse than pointless. `/api/timeline?run=B-control` returns
`total_events: 0` — 1,800 ledger rows, nothing replayable, because the arm took no actions.
`play()` sees `cursor >= events.length`, flips the label to `Pause`, and `tick()` immediately
stops it again: the button flickers and nothing happens. That reads on camera as an app that
ignored a click, in the section about whether the numbers can be trusted.

On a **case** shot it actively destroys the frame. `restart()` sets `state.caseId = null` and
replaces the case panel with *"Select Play, or choose an entry from the decision list"* — so
the trail being discussed disappears mid-sentence.

The screen already says the thing without help. *"No actions recorded. This arm did nothing
at all — which for the control arm is the entire point"* is the shot.

**If you press Play while setting up, reload the URL before recording.** `step()` increments
the counter row as it plays, so a stream that was started and stopped leaves `cases worked`,
`charges presented` and the rest showing partial totals that match nothing in the README —
and reading a stray number off a screen is exactly the failure this project is built to
avoid. Use a plain reload, not `Restart`: Restart zeroes the counters but also nulls
`state.caseId` and empties the case panel with it.

**What Play actually shows, if you ever want it elsewhere.** Not sample data — the feed is
read back from `data/<batch>/ledger.db` row by row, newest first, which is why entries appear
at the top and push the rest down. Per arm: naive 3,000 entries, agent 2,340, control 0. The
speed slider defaults to 18, and the interval is `max(8, 900 / speed)` — 50ms, or 20 entries
a second, which is a firehose, and two minutes to reach the end. Drag speed to **1** for
900ms per entry. On the naive arm at that pace the red `charged twice` rows arrive one at a
time and can actually be read. It is a good shot for a longer cut. There is no room for it
in five minutes, and no line in the script to carry it.

---

## Two things worth resisting

**A feature tour.** The strongest thing here is the control arm, and it is the one thing no
other submission will have. Time spent on the console's UI is time not spent on it.

**Re-recording to fix a stumble in the middle.** The spine is "why you can believe the
number", at 1:30 — 2:32. If that section is clean, a stumble elsewhere is survivable. If it
is not, the take is not worth keeping however good the rest was.
