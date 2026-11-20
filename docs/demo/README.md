# Replays of real runs

The README's GIFs, the tour video on oneyerge.com and the browser demo at [oneyerge.com/demo/ybm](https://oneyerge.com/demo/ybm/) all come from one page in this folder. The page draws a phone and the computer from the logs of real runs. Every message, file name, number and real-time stamp on screen is read from a saved run; nothing is typed in by hand.

## What each replay comes from

| Replay | Run | What is real |
|---|---|---|
| Find | `v_steam`, 19 November 2026, on the real Telegram bot | The question, every message the phone received and when, the tool and its output (14 games, about 203 GB) |
| Organize | The run behind the earlier README demo ([`docs/screenshots/demo-run.json`](../screenshots/demo-run.json)) | The move list the model wrote (24 files, 7 folders), the console's progress lines, the approval text, the final answer, and the clock of that recording |
| Build | `v_build_tip_calculator`, 19 November 2026, on the real Telegram bot | The request, every message and when, and the page itself: the Build replay shows and runs the `index.html` the run wrote |

The model in all three is Qwen3.5 9B (4-bit, llama.cpp) on an 8 GB laptop GPU.

## What is drawn

- The phone is a plain chat view, not the Telegram app. The spinner on a progress message is drawn.
- The computer side shows what the run did, not a screen recording: the file tiles, the folders, the tool's output as a list, and the code as it was written. The file and folder icons are drawn.
- Waiting is played faster. The real-time clock and the speed (for example "10x speed") are on screen whenever time is compressed.
- In Build, the part after the link is marked "after the run": the page the run wrote is opened and used (64.50 with an 18 percent tip gives 76.11, computed by that page).
- In Organize, the approval was tapped by the recording script about 4 seconds after it appeared. In the browser demo, the visitor taps it.

## Making them again

The data comes from the result folders of a live run (`scripts/run_all_e2e_tests.py`), which stay on the machine that ran them:

```
python docs/demo/build_data.py <path to .agent_control/e2e_results/run_...>
```

`build_data.py` writes `stage/stories.js`. To record a replay, install the console's dependencies once (`cd frontend && npm ci`), then:

```
node docs/demo/record.mjs organize organize.mp4        # 30 frames a second, 1920x1080
node docs/demo/record.mjs find find.mp4
node docs/demo/record.mjs build build.mp4
node docs/demo/record.mjs intro intro.mp4
node docs/demo/record.mjs outro outro.mp4
```

The recorder opens `stage/record.html` in headless Edge, sets the time for each frame, takes a screenshot and pipes it to ffmpeg, so a replay looks the same however fast the machine is. `NOCAP=1` leaves out the captions (the oneyerge.com card uses that).

The tour is the five scenes joined with half-second fades:

```
ffmpeg -i intro.mp4 -i find.mp4 -i organize.mp4 -i build.mp4 -i outro.mp4 -filter_complex "[0][1]xfade=duration=0.5:offset=6.0[a];[a][2]xfade=duration=0.5:offset=19.5[b];[b][3]xfade=duration=0.5:offset=44.0[c];[c][4]xfade=duration=0.5:offset=68.5,format=yuv420p" -c:v libx264 -crf 20 -movflags +faststart ybm-tour.mp4
```

The GIFs are 1280 pixels wide, at 15 frames a second (Build at 12, to keep it near 5 MB):

```
ffmpeg -i organize.mp4 -vf "fps=15,scale=1280:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=220:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" docs/screenshots/ybm-organize.gif
```

`node docs/demo/test_web.mjs` plays the browser demo at desktop and phone width, taps the approval, and uses the page in Build, to check that the page still works.

## Rules for a new replay

- Use a run that passed. Say which run it is, in the caption or in this file.
- Keep every message as the run sent it. If a message reads badly, fix YBM and record a new run.
- Show the real-time clock, and the speed whenever it is not 1x.
- Mark anything that happened after the run, or was done by the recording rather than the run.

The fonts are Geist (SIL Open Font License, `stage/fonts/OFL.txt`). Code is shown in Cascadia Mono when the computer has it.
