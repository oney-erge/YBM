# Agent model benchmark

Which local model should run YBM's Operator? A general quality score does not answer that: the model YBM used
until now ranks first in LocalDeploy's general benchmark and still made up file names and job ids in live runs. This
benchmark measures the Operator's actual job, using the decisions YBM has already made.

## What it measures

Every Operator call YBM makes is stored with its exact prompt and answer, and the task keeps what each call led to.
`steps.py` turns that into labeled cases:

| Recorded decision | Outcome | Label |
|---|---|---|
| a tool call | it succeeded | good |
| a tool call | it failed, was denied, or was an unneeded repeat | bad |
| `done` | rejected by the Auditor or a fulfillment check | bad |
| `done` | the task completed | good |
| a question to the user | rejected by the clarification check | bad |

Anything else (approvals, delegation, calls whose outcome cannot be matched) is left out rather than guessed.

`replay.py` sends each case's stored prompt to a candidate model exactly as YBM does (the same `OperatorDecision`
JSON schema as `response_format`, temperature 0.1, 4096 max tokens) and `grade.py` scores the answer:

- **valid call**: the tool exists and, after YBM's own call normalization, its input schema accepts the input
- **no made-up references**: every file path, URL and id in the call appears in what the model was shown. A JSON
  grammar keeps the shape valid; it cannot stop a plausible file name from being invented
- **same as good step**: on a step that went well, the model chose the same action, tool and operation
- **avoids bad repeat**: on a step that went badly, the model does not make the same call again
- **pass**: all of the above that apply
- **latency, tokens per second, VRAM**: what the model costs on this machine

## Run it

Needs `llama-server` (`winget install ggml.llamacpp`), GGUF files for the candidates, and the YBM checkout whose
`config/config.yaml` defines the tools. Nothing here starts YBM's services or sends anything off the machine.

```powershell
$env:PYTHONPATH = "<repo>\backend\src"; Set-Location <ybm checkout>   # config.yaml and the database are read from here
python <repo>\scripts\agent_benchmark\steps.py  --db .agent_control\agent_control.db --count 120 --out steps.json
python <repo>\scripts\agent_benchmark\matrix.py --models-dir D:\LocalModels\gguf --steps steps.json --out results
python <repo>\scripts\agent_benchmark\report.py results
```

`candidates.py` lists the models and the llama-server settings (flash attention, 8-bit KV cache, thinking off, 16K
context). Layer placement is left to llama-server's `--fit`; mixture-of-experts models are also run with the experts
pinned in RAM, as a short probe. `matrix.py --force --full-variants --only "<name>"` replays every step for the best
placement. A model whose first eight steps have a median above 40 s is stopped and recorded as too slow.
`replay.py --url http://127.0.0.1:8000/v1 --model <profile>` scores any OpenAI-compatible endpoint, such as the
production path through LocalDeploy and Ollama.

## Limits

- One attempt per step. YBM retries a failed answer up to three times; here a model that needs the retry fails the step.
- Labels come from outcomes, not from a person. A step can be "bad" for a reason that is not the model's fault, and
  there can be more than one good answer; agreement with the recorded good step is therefore a floor, not a ceiling.
- It replays single steps. It does not show how a model recovers across a whole task; the E2E suite does.
- The cases are the owner's own history. The prompts contain their messages and file names, so they stay in the local
  database and the scratch results. `report.py --json` writes only scores and timings.
