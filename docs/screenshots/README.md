# Screenshots

`demo.gif` is the README's opening demo: a real run, replayed. Qwen3.5 9B (on
a laptop GPU through llama.cpp) organized a sample Downloads folder of 24
placeholder files in 44 seconds. YBM logged every model call of that run, and
[`demo-run.json`](demo-run.json) holds them: each prompt, the model's exact
response, and how long it took. For the recording,
[`scripts/replay_model_server.py`](../../scripts/replay_model_server.py) serves
those responses as the model, each after its recorded time. Everything else is
live: YBM, its tools, the files it moves, the approval, the console. A response
is served only in the situation it was recorded in (its role and the steps
taken before it), so the replay cannot show a run that went another way.

The phone beside the console is a plain chat view of the same approval (no
real account); tapping Approve there approves it through the console's API.
Long model waits play faster, with the speed marked on the frame, and a clock
above the phone shows real time.

A replay keeps the GIF reproducible without a GPU: a shared or busy GPU made
the same model 20 times slower in a direct live take, which would misstate how
long the work takes.

## Regenerating demo.gif

The recorder, [`frontend/e2e/demo.spec.ts`](../../frontend/e2e/demo.spec.ts),
has two modes. Both write frames, and
[`scripts/make_demo_gif.py`](../../scripts/make_demo_gif.py) turns them into
the GIF.

**Live** records a running YBM. It needs a model, and a
Downloads folder granted with write access and approvals on. Use a sample
folder, not your real Downloads: the run really moves the files.

```
cd frontend
YBM_RECORD_DEMO=1 YBM_DEMO_LIVE_URL=http://127.0.0.1:8765/admin/ \
  YBM_DEMO_CAPTION="A real run: <model> on <hardware>. ..." npx playwright test demo.spec.ts
cd .. && backend/.venv/Scripts/python scripts/make_demo_gif.py
```

For the committed GIF, the model is the replay of `demo-run.json`: serve it,
point the instance's model profile's `base_url` at `http://127.0.0.1:8781/v1`,
start from an empty database and a sample folder holding the same 24 files,
and record live as above. To replay a new run instead, export it from the
database it ran in:

```
python scripts/replay_model_server.py serve docs/screenshots/demo-run.json --port 8781
python scripts/replay_model_server.py export .agent_control/agent_control.db <task id> \
  docs/screenshots/demo-run.json --replace "<your sample home>=C:\Users\sam"
```

**Scripted** mocks every API response, so it costs nothing, needs no model, and
produces the same frames on any machine:

```
cd frontend && YBM_RECORD_DEMO=1 npx playwright test demo.spec.ts
cd .. && backend/.venv/Scripts/python scripts/make_demo_gif.py
```

- `demo.spec.ts` owns *what the console shows*: the story beats, and in
  scripted mode every response behind them.
- `make_demo_gif.py` owns *how long each beat is held*, and plays a live
  recording's long waits faster. Re-timing the demo does not mean re-recording
  it.

Frames land in `frontend/.demo-frames/` (gitignored); only the assembled GIF is
committed.

## Why the story is fixed

An earlier GIF was captured from a free-form live session, which made it
unreproducible and pinned the story to whatever that run happened to do. It
ended up being the same trivia question asked several times, which sells YBM as
a chat window rather than an agent that works on your computer. Both modes now
drive the same story; in live mode the work in it is real.

The beats are chosen to say the opposite, in order:

| Beat | What it has to land |
|---|---|
| Empty chat | Every suggestion is work on this machine, not a question to answer |
| Typing, sending | A person asks in their own words |
| Working | It is doing something real, and says what |
| **Approval** | It stops and asks *before* touching files - the longest hold in the GIF |
| Done | A receipt naming every file it moved, and that nothing was deleted |

If you change the demo, keep the approval beat. It is the difference between
"an agent ran a command" and "an agent asked first".
