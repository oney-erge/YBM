---
name: Plan a project
description: turn a big goal (build a robot, set up a home server, plan a trip, launch a site) into a plan the user can start; ask what they want first when the goal is vague
tools:
  - plan.manage
---
Use this when the request is a goal that takes days or many separate steps, or that includes things only the user can do (buying,
building, deciding, signing). A one-step request is a task, not a plan.

1. If the goal is vague (no purpose, size or budget), ask the user ONE question first with `ask_user`. Offer two or three concrete choices in
   the question itself ("Small indoor drone, outdoor camera drone, or something else?"). Do not draft a plan from a guess.
2. Once you know enough, write 4 to 10 steps. One thing per step, in order, in plain words: "Compare three flight controllers", not "Research".
3. Give every step an owner:
   - `ybm`: anything you can do on this computer: research, comparing options, writing code or documents, making lists, drafting messages.
   - `user`: anything physical or that costs money or needs a decision: buying, assembling, testing, signing, choosing between options.
   - `access`: you could do it if the user gave you something, such as an account, a key or a login. Put what you need in `needs`.
4. You never pay or place an order. Plan it as two steps: you make the shopping list with links (`ybm`), then the user buys (`user`).
5. Call `plan.manage` with `operation` create, the `goal`, a one or two sentence `summary` (what done looks like), and the `steps`. A step that
   cannot start until another is done names it in `depends_on` (positions of earlier steps).
6. Answer with the plan in a few short lines and ask the user to tap Start. Do not start any step yourself.
7. When the user changes their mind later ("make step 3 the garage instead", "I bought the parts"), use `plan.manage` update_step or
   add_step, and say what changed.
