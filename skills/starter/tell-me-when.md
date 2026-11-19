---
name: Tell me when something changes
description: watch a folder or a web page in the background and message the person once for each change, instead of answering about it now
when: Is the user asking to be told later when something changes or appears (a new file in a folder, a web page that changes), rather than asking about it now?
when_at: 0.75
route: job
tools:
  - job.manage
---
Use this when the person wants to hear about a change later: "tell me whenever a new file shows up in that folder", "let me know if that page changes". A question about what is there now is not this: answer that yourself.

1. The first step is made for you: a watch is started on the folder or page the person named, looking every 15 minutes unless they said how often. If it was not, call `job.manage` with operation `start_watch`, `target` the folder path or the page address exactly as they wrote it, and `every_minutes` only if they gave one.
2. The person approves it first. The first look only records what is there: nothing is announced for what already exists.
3. Tell the person in two sentences that it is watching, how often it looks, that it will say something only when there is a change (and when it cannot look for a while), and that they can pause or stop it. Do not promise it works while the computer is off.
4. For "pause the watch", "stop watching" or "how is the watch going?" call `job.manage` with operation `pause`, `stop` or `status`.
