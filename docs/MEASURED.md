# Measured results

This page lists what I have measured about YBM, on one machine with one model, and what I have not measured. Each suite named here is a file in the repository, so a result can be rerun and checked.

## How it was measured

A suite is a fixed list of requests written in plain words. Each request is sent to the running assistant through its real Telegram channel. Code then checks the outcome: the reply, the files that changed, and the approvals that were asked. Nobody reads an answer and decides afterward. Some checks look for words in the reply, so a strict check can fail an answer that is correct.

- **Machine:** a Windows 10 laptop with an 8 GB NVIDIA RTX 3080 laptop GPU.
- **Model:** Qwen3.5 9B, 4-bit (Q4_K_M), served by llama.cpp with a 16K-token context. A smaller model that sorts incoming messages runs on the CPU.
- **Setting:** the highest level of "How much it asks" (level 3), which does not ask and grants routine approvals itself. The test runner approves what a run asks for. It does not check that a person would.
- **Author:** I wrote the assistant and the suites, and I ran them. These are my own results, not an independent review.

To rerun a suite, see [e2e/README.md](../e2e/README.md). A suite takes from several minutes to about an hour on this model.

## Results of 19 November 2026

| Suite | What it asks | Passed | Misses |
|---|---|---|---|
| Everyday | 32 requests: find files, count the Steam games, read five websites, build and run a small app, say "I cannot" when that is true | 30 of 32 | `v_claude_code_on_request`, `v_cannot_order` |
| Verdict 4 | 20 requests, sealed before the work and run once (target 18) | 15 of 20 | five, listed below |
| Ultimate | 14 requests: a plan for a pool, a phone, a device on the network, retrying after a failure | 12 of 14 | `ult_phone`, `ult_try_again` |
| Hands | 10 requests that use a browser or the desktop | 5 to 9 of 10 across five runs | see below |
| Memory | 8 requests: a fact comes back, a fact that changes keeps its history | 8 of 8 on the last run | 7 of 8 on the run before it |

Rows are the last full run of each suite on that day. The suites changed and were fixed between runs, so a row is a snapshot of one build. It is not a trend.

**Everyday.** `v_cannot_order` has passed when run alone since a later fix (24 seconds), but the whole suite has not been rerun. The check for `v_claude_code_on_request` could not inspect the file that the task produced.

**Verdict 4.** I sealed these 20 requests (their hash is in [e2e/verdict4_cases.sha256](../e2e/verdict4_cases.sha256)) before writing the fixes the exam would test, so I could not tune the assistant to its wording. It scored 15 of 20, below my target of 18. The misses were:

| Case | What happened | Status |
|---|---|---|
| `v4_plan_revise_twice` | The claims check took "will be generated" for a finished write, asked twice, and the blocked task offered to hand the work to Claude Code | Fixed in [#299](https://github.com/oney-erge/YBM/pull/299) |
| `v4_fact_moves` | "I live in Dallas." took 3.5 minutes to become a task, because the model filled an unused field with invented text | Fixed in [#300](https://github.com/oney-erge/YBM/pull/300) |
| `v4_guarantee_paper` | A question about a date started a folder watch instead of reading the paper | Fixed in [#301](https://github.com/oney-erge/YBM/pull/301) |
| `v4_script_rename` | A code-writing step ran 12 minutes with no model call logged; the cause is not proven | Open |
| `v4_retry_missing_notes` | The reply did not say that the notes were missing | Not yet traced |

I have not rerun the exam, because a sealed exam is run once. The three fixed misses were each traced to a cause. That is not the same as a second exam score.

**Hands.** The cases that open Notepad and the calculator need an unlocked screen and a model that reads images. On the evening of 19 November the screen was locked and the image model returned errors, so those two were cancelled. That run scored 6 of 10. Two other misses (a download and a search on a page) were fixed afterward and passed when run alone.

## Speed

| Measurement | Value | Source |
|---|---|---|
| Reading the prompt | about 1,500 tokens per second | measured on 14 November |
| Writing the answer | about 44 tokens per second | measured on 14 November |
| Time until the answer reached the person, Verdict 4 | median 37 seconds, from 6 to 104 | 19 November, 19 of 20 cases |
| Organizing a folder of 24 files | 44 seconds, with one approval | the run behind the README demo |

The time is measured from the person's message to the last reply that is not an edit of a progress line. Verdict 4 has no research case. A research job runs for minutes to hours and sends progress messages.

## What I have not measured

- **Other people's machines.** No one except me has used YBM for a task that I know of. I do not know how it behaves on a different GPU, a different model, or a different language.
- **macOS and Linux tasks.** On every change, continuous integration starts YBM and passes a health check on Windows, macOS, and Linux, and replays 16 recorded task scenarios (find a file, summarize a PDF, run code, schedule a job, call a tool server) on all three. No task has been run against a real model on macOS or Linux. Desktop control exists only on Windows.
- **Long runs.** A three-day run with a restart has not been done. A 600-page research run produced a thin report: the claim check removed 178 of 238 cited sentences. A survey of daycares filled 12 percent of its table cells, and the fix for that has not been rerun. See [PLAN.md](PLAN.md), section 2.
- **Memory across days.** Memory has passed its suite, but a fact remembered across weeks of real use has not been shown.
- **Comparisons.** I have not run these requests against any other assistant, so I make no claim about how YBM compares.

## Test suite of the code

The backend has about 3,500 automated tests (3,477 collected on 19 November) that run in about 40 seconds on a 16-core machine. They use recorded model replies, so they test the code and not the model.
