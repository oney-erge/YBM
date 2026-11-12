# Screenshots

`demo.gif` is the README's opening demo, recorded in scripted mode (below).

Live mode records a real run instead: a local model organizes a sample
Downloads folder, nothing in the console is mocked, and the phone beside it is
a plain chat view of the same approval (no real account). Tapping Approve there
approves it through the console's API. The model's thinking is played faster,
with the speed marked on the frame, and a clock above the phone shows real time.

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
