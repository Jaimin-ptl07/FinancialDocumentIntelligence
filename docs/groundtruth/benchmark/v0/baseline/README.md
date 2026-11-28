# Today's `/ask` baseline (S1.8, #47)

The first full measurement of `/ask` against benchmark `v0`, before any of
the agent-system specs (S2..S10) change it. Every later run is compared
against these files with `scripts/eval_answers.py diff`.

| File | What |
|---|---|
| `answers.json` / `answers.md` | `run --yes --judge`: all 96 items answered through `/ask`, the 11 research items also judged |
| `retrieval.json` / `retrieval.md` | `run --mode retrieval`: hybrid retrieval alone, 86 items with gold evidence (no model call) |

## Stamp

- **git commit**: `594fa84` (both reports)
- **model deployment**: `gpt-4.1-invoice-processing` (Azure OpenAI, model
  `gpt-4.1`) for both `/ask` and the judge. The judge's stamp names it; every
  `/ask` usage row of this run was billed as `gpt-4.1` under that deployment.
- **corpus**: `tcs-2025-26`, `tcs-2024-25`, `ril-2025-26`, `rites-2024-25`
  (REMC), `sttl-2023-24-ex1`. Benchmark `v0` only asks about the first,
  third and fourth; the others are distractors that retrieval must step past.
- **run**: 2026-11-26. The first pass answered 55 items, then the eval
  Invitee's 15-minute access token expired and the remaining 41 got 401s.
  `594fa84` signs in again on expiry and adds `--resume`; the 55 were re-read
  from their jobs (answers, citations, usage, latency as first measured) and
  only the 41 were asked. The `resumed` stamp field records this; its path
  is the local scratch report, not committed.

## Headline

| kind | items | accuracy | completion | citation validity | Recall@5 | Recall@10 | MRR | P50 / P95 latency (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| fact_lookup | 59 | 8.5% | 3.4% | 0.2% | 13.6% | 40.7% | 0.115 | 14.3 / 19.6 |
| calculation | 10 | 0.0% | 0.0% | 0.2% | 20.0% | 40.0% | 0.074 | 14.7 / 31.1 |
| cross_company | 6 | 0.0% | 0.0% | 0.1% | 0.0% | 0.0% | 0.000 | 19.7 / 35.8 |
| research | 11 | 27.3% | 18.2% | 0.3% | 54.5% | 72.7% | 0.437 | 15.8 / 23.9 |
| unanswerable | 10 | 60.0% | 60.0% | - | - | - | - | 10.2 / 22.1 |
| **overall** | 96 | 14.6% | 10.4% | 0.2% | 18.6% | 41.9% | 0.143 | 14.4 / 23.9 |

Cost: `/ask` INR 595.41 for 96 items; 10 completed, so **INR 59.54 per
completed task**. The judge's INR 34.70 is eval spend, not in that figure.
Research claim support (judge): 11 of 31 claims, 2 of 11 items fully
supported.
