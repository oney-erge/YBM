---
name: Work out why something does not work
description: run an investigation job (read-only checks of this computer and the home network, what is known on the web, then ways forward) when the person has a problem that has to be found out, such as a device that will not connect or a program that stopped working
when: Is the user describing something that does not work on their computer, network or a device (it stopped working, will not connect, is slow, keeps failing) and asking you to find out why or to fix it?
when_at: 0.6
route: job
tools:
  - job.manage
---
Use this when the person has a problem whose cause is not known: a TV that will not take a cast, a printer that stopped, a Wi-Fi that drops, a program that will not start. A question you can answer from one setting or one fact is not this: answer that yourself. Research on a topic is not this either.

1. The first step is made for you (the job is started with the person's problem in their own words). If it was not, call `job.manage` with operation `start_investigation` and the whole problem in `goal`: what does not work, what it is connected to, and anything they already tried.
2. The person approves it first. It only reads this computer and the home network; it changes nothing. Do not run the checks yourself in this task.
3. Once it has started, tell the person in two sentences that you are checking their computer and network and looking up what is known, and that you will message them what you found and the ways forward. They can ask "how far are you?", pause it or stop it.
4. When it has finished, the person picks a way forward ("try 2", "you choose") and that becomes a task: do it with the tools you have, ask before anything that changes a setting, say how to undo it, and check afterwards that it worked. If one way fails, try the next one yourself; do not stop at the first failure.
