---
name: Plan a project
description: turn a big goal (build a robot, set up a home server, plan a trip, launch a site) into a plan the user can start; ask what they want first when the goal is vague
when: Is this a big goal or project that takes many separate steps or days (building or setting up something, planning a trip or an event) rather than one quick request?
when_at: 0.8
route: plan
tools:
  - plan.manage
---
Use this when the request is a goal that takes days or many separate steps, or that includes things only the user can do (buying,
building, deciding, signing). A one-step request is a task, not a plan.

1. If the goal is vague (no purpose, size or budget), ask the user ONE question first with `ask_user`, offering two or three concrete choices in
   the question itself ("A small indoor drone, an outdoor camera drone, or something else?"). Ask at most once: after the user has answered,
   never ask again.
2. Then call `plan.manage` with `operation` create and the `goal`, written out in full with everything the user said (what it is, size,
   budget, anything they named). Do not write the steps yourself: a planner drafts them, gives every step an owner (you, the user, or you once
   the user gives you something) and writes down its assumptions, so the user can correct the plan instead of answering more questions.
3. Answer with the plan in a few short lines and ask the user to tap Start. Do not start any step yourself.
4. When the user changes their mind later ("make step 3 the garage instead", "I bought the parts"), use `plan.manage` update_step or
   add_step, and say what changed.
