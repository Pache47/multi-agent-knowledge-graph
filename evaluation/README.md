# Evaluation

**Question:** does the knowledge graph help an AI assistant give technicians correct,
safe answers, compared with the same agents searching the original files?

Three layers, each measured separately: a good answer can hide a bad graph, and a good
graph can still produce a poor answer.

| Layer | What is measured | How |
|---|---|---|
| 1. Construction | Is the graph built correctly from the messy folder? | 53 hand-written gold checks ([*_gold.json](.)) plus repeated fresh live builds |
| 2. Answers | Does the assistant answer correctly and safely? | 32 rubric cases ([cases.json](cases.json)), each with required points and material errors, graded by a judge model |
| 3. Baseline | What does the graph add? | Same cases, same answer model, responder and verifier, but the investigator gets only search/read over the original files ([baseline.py](../src/factory_assistant/baseline.py)) |

No agent ever reads `evaluation/`.

## Results (27 Oct 2026, with the source-text fallback)

gpt-4.1-mini answering, gpt-4.1 judging, 32 cases, 2 runs each
([report](runs/2026-10-27-stock/report.md)). The graph system falls back to one bounded
search of the source text when the graph holds nothing on the question; a file whose
machine is unresolved comes back labelled as a held record.

| Cases | Arm | Passed answers | Required points | Material errors |
|---|---|---|---|---|
| **All 32** | **Graph + fallback** | **45/64** | **141/170** | **1** |
| | Documents only | 32/64 | 125/170 | 8 |
| Round 3, first run (8 cases unseen until scored) | Graph + fallback (model-chosen) | 8/16 | 22/34 | 2 |
| | Documents only | 12/16 | 30/34 | 0 |
| Round 3, final (tuned on) | Graph + fallback | 12/16 | 27/34 | 0 |
| | Documents only | 8/16 | 26/34 | 1 |

**Construction:** 53/53 gold checks on the replayed build.

**How we got here, and what it shows**

1. **The first fallback did not work.** When the investigator could choose
   `search_documents`, it did so in 10 of 64 answers and never for the two cases it was
   built for (X07, X08). That run is kept as the clean held-out result
   ([report](runs/2026-10-27-fallback/report.md)): on the new cases document search was
   ahead.
2. **Running the fallback in code closed the coverage gap**
   ([report](runs/2026-10-27-final/report.md)), but it brought 2 material errors on Y06.
   Y06 asks about a callout whose machine is unresolved; that file was left out, and the
   responder filled the gap with records from other dates.
3. **Showing the held record fixed that.** The fallback now returns the callout labelled
   "held record: machine not identified", so the answer says the callout exists, which
   machine is unknown, and that its work and result are missing. Y06 now has no material
   errors.
4. **Stock answers were made clearer.** "Do we have it in stock?" got an odd question back
   ("can you confirm the stock records?") because claims may not state counts and the
   count sat in a folded box. Looked-up stock is now always shown as its own card, and the
   responder may not ask the technician to find records
   ([report](runs/2026-10-27-stock/report.md); the run before it,
   [held](runs/2026-10-27-held/report.md), passed 48/64 with 1 material error, which is
   within run-to-run variation).
5. **Overall** the graph system passed 45/64 answers with 1 material error, against 32/64
   with 8 for the same agents using document search. Document search keeps making the
   errors the graph prevents: a motor replacement recommended on a guessed machine (A01),
   an unresolved callout assigned to a machine (Y06), an old setup said to match today's
   (H01). The round-3 cases were seen during these fixes, so their final numbers are
   tuned. Document search varied a lot between runs (4 to 10 material errors on the same
   cases), which shows how small this sample is.
6. **The remaining graph error** is H02 run 2: it says CV-01 had a motor replacement,
   which was CV-02's job, and the verifier missed it. Earlier runs' single error was live
   status inferred from a log ("so the conveyor is currently stopped again", L01). Median
   answer time is about 10 s for both arms.

## Earlier results (26 Oct 2026, graph tools only)

24 cases, 2 runs each ([report](runs/2026-10-26-final/report.md)).

| Cases | Arm | Passed answers | Required points | Material errors |
|---|---|---|---|---|
| All 24 | Graph system | 29/48 | 104/136 | 0 |
| | Documents only | 26/48 | 100/136 | 6 |
| Held-out (8) | Graph system | 6/16 | 26/44 | 0 |
| | Documents only | 9/16 | 33/44 | 0 |

The graph made no material errors but was less complete on unfamiliar questions: facts
outside the knowledge model (a stores-file caveat in X07, a shift-note remark in X08)
were unreachable. Tuning inflates scores: on the cases used while tuning, the graph
system passed 11/12. The failure analysis below is from this run.

## Failure analysis (graph system, 26 Oct)

| Pattern | Cases | Likely fix |
|---|---|---|
| Fact not in the knowledge model, so unreachable | X07, X08 | Source-text fallback (done 27 Oct) |
| Wrong tool for the question | X04 (events instead of history), P01 (no stock lookup) | Clearer tool descriptions; a routing example per question type |
| Asks which machine when the question already says | T03, H04 | Search the referenced record before clarifying |
| Correct but incomplete applicability claims refused | X05 | Cite the path card automatically for applicability claims |
| Withheld after failed verification | X07, X08 (3 of 16 held-out answers) | Release the verified records with a short note instead |

## Changes between runs

[Run 1](runs/2026-10-26-first/report.md) (16 cases) showed the graph system passing 9/20 on
then-unseen cases against 12/20 for documents. Three general fixes followed, and those
cases are now marked as used for changes:
- maintenance records became citable by the responder;
- an unknown event code now returns the codes that exist, not "0 events";
- the investigator looks up a referenced record before asking which machine.

The 8 held-out cases were written and committed before those fixes (commit `755c308`).

On 27 Oct, 8 round-3 cases (Y01–Y08) were committed before the fallback was scored, and
X07 and X08, which shaped the fallback, were marked seen. After the first fallback run,
two changes followed: code runs the fallback when the graph finds nothing (with rules
that source-text passages may only be used for what they state themselves), and a file
with an unresolved machine comes back labelled as a held record instead of being left out.
Last, stock answers were changed (stock card always shown, no questions asking the
technician for records) and the full set was scored again.

## Caveats

- The judge model is fallible. A spot check found its verdicts generally right but strict
  (for example, naming the manual's file instead of "CT200-B revision 3" fails a point).
  One probable false positive: L01 run 2 was counted as stating live status, but it gave
  the last recorded state with its time.
- 32 cases × 2 runs on 15 synthetic files is a small sample, and live models vary between
  runs. Differences of a few answers are not significant.
- This measures answers, not technician time saved. That needs task trials with
  technicians: time to a decision, documents opened, and errors.

## Running it

```sh
# Spends API credits (~4 minutes for 32 cases × 2 arms × 2 runs)
PYTHONPATH=src .venv/bin/python tools/evaluate.py --output evaluation/runs/NEW --repeats 2
PYTHONPATH=src .venv/bin/python tools/evaluate.py --rerender evaluation/runs/NEW
```

Each run writes `results.json` (answers, traces, judge verdicts, token usage) and
`report.md` to a new folder; earlier runs are never overwritten.
`tools/run_examples.py` prints readable answers for a few questions without scoring.
