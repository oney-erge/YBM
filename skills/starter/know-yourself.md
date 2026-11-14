---
name: Say what this assistant can and cannot do
description: answer a question about the assistant itself (what it can do, cannot do, is missing, needs from the user, or what is new) from its ability index, not from memory
when: Is the user asking what this assistant can do, cannot do, is missing, or needs from them to be more useful?
when_at: 0.5
ask: always
tools:
  - ability.search
---
Use this when the question is about the assistant itself, however it is worded: "what can't you do?", "what do you need from me to be more useful?", "what can you do now?", "what's new?". The question is complete: do not ask the user what they mean or which abilities to explain.

1. Call `ability.search` once with operation `overview`. Do not answer from memory, and do not call other tools for this: what the assistant can do changes with what is installed and switched on.
2. Answer from what it returns, in a few short plain lines, with no tool names or ids:
   - "What can't you do?": the things it lists as cannot be done, each with the nearest thing that can be done.
   - "What do you need from me?" or "what are you missing?": the concrete things it lists as could be set up, switched off, or needing an account, what each would unlock, and which two or three you recommend first. Offer to set them up.
   - "What can you do?" or "what is new?": what is ready, grouped by what it is for, and the skills and kept scripts it lists. Say that you do not keep dates of when something was added.
3. Never claim an ability the overview does not list.
4. This is a question, not a request to change anything: do not install, switch on or set up anything in this task. End with an offer ("say the word and I will set up the voice") and stop; do not ask a question that holds the task open.
