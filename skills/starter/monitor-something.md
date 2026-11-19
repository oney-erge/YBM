---
name: Keep watch on something on this computer or network
description: write a small read-only script that looks at something on this computer or the home network (devices, disk space, a service, a drive), keep it, and have it checked every few minutes, telling the person only when something changes
when: Is the user asking you to keep monitoring something on their computer or home network over time (devices, services, disk space, open ports, a program) and tell them when it changes?
when_at: 0.6
route: job
tools:
  - job.manage
  - code.interpreter
---
Use this for "connect to every device on my network and tell me when a new one shows up", "warn me if the D drive gets under 20 GB", "tell me if the NAS goes offline". There is no tool for these: you write the script, the tool saves it, tests it once, and a background watch runs it. A question about how things are now ("how many devices are on my network?") is not this: answer it.

1. Write a Python script, standard library only, that LOOKS and never changes anything (no deleting, writing, installing, restarting, or touching other machines beyond reading what they announce). It prints one line for each thing it sees, the same way every time, sorted: for devices `ip mac name`, for a drive `D: 412 GB free`, for a service `Spooler Running`. Print what identifies a thing, not what changes all the time (no timestamps, no counters), or every look will look like a change. Keep it short and give it a time limit of its own (a few seconds per network call).
2. Devices on the home network, a good start, tried on a real PC and it printed the devices: `import re, subprocess`, run `arp -a`, and for each line matching `\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{2}(?:-[0-9a-f]{2}){5})\s+dynamic` that is not a broadcast or multicast address (a MAC starting `ff-ff` or `01-00-5e`) print `ip mac`; for names try `socket.gethostbyaddr`. Do not scan outside the person's own network, do not try passwords, do not probe ports beyond a short list of the common ones, and stay under the person's own subnet. Anything more intrusive is a different request and waits for a yes.
3. Start the watch with `job.manage` operation `start_watch`, `script` (the code), `name` (a plain name such as `network-devices`), `goal` (the person's request in their words) and `every_minutes` (how often they said; otherwise 10 for devices, 60 for disks). The tool saves the script, runs it once as a test, and refuses with the reason if it fails or prints nothing: fix it and call again, up to three times. Do not ask the person to write or test anything.
4. Tell the person in two sentences what it watches and how often, that the first look only records what is there, and that you will say something only when it changes. Say what the script sees now (the first run's lines), so they can tell it is right.
5. To change what it watches, write the script again under the same name; to stop, use `job.manage` operation `stop`.
