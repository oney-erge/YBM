---
name: Research a topic in depth
description: run a long research job (many web pages read one at a time, a cited report at the end) when the person wants a topic researched thoroughly, instead of answering from a few searches
when: Does the user want a topic researched thoroughly across many sources, with a report or answer to come back to later?
when_at: 0.6
route: job
tools:
  - job.manage
---
Use this when the person wants research done properly: many sources, "read as much as you need", "come back to me", a report. A quick lookup is not this: answer that yourself.

1. The first step is made for you (the job is started with the person's goal and the number of pages they named). If it was not, call `job.manage` with operation `start_research`. Put the whole goal in `goal`, with everything the person said (the topic, the place, the budget, what they care about). If they named a number of sources, set `max_pages` to it (at most 150); otherwise leave it out.
2. The person is asked to approve it first. That is how they agree to hours of work, so do not do the research yourself in this task. Ask a question first only if the goal lacks something that would change what to search for, and then one question at most.
3. Once it has started, tell the person in two sentences that it is running in the background, that messages come as it goes and the report at the end, and that they can ask "how far are you?", pause it or stop it. Do not promise a time.
4. For "how far are you?", "pause the research", "resume it" or "stop it", call `job.manage` with operation `status`, `pause`, `resume` or `stop`.
