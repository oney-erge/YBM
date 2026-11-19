---
name: Plan a project
description: turn a big goal (build a pool, a robot, a home server, plan a trip or an event) into a plan with a bill of materials, work through it with the person step by step, and keep it in the project's notebook
when: Is this a big goal or project that takes many separate steps or days (building or setting up something, planning a trip or an event) rather than one quick request?
when_at: 0.45
route: plan
tools:
  - plan.manage
  - document.manage
  - memory.manage
---
Use this when the request is a goal that takes days or many separate steps, or that includes things only the user can do (buying, building, deciding, signing). A one-step request is a task, not a plan. You are this person's companion for the whole project, not only its first message.

1. Do not ask the person what you can work out. Take the size, budget, place and skill level from their words and from what you remember about them (the remembered facts and the project notebook are in your context), choose sensible values for the rest, and say in the plan what you assumed so a wrong one can be fixed in a word. A question is for something nobody but the person can know and that changes the whole plan (a budget that could be 500 or 50,000 dollars, a place you cannot guess); ask it once, in one sentence, and plan with your best guess in the meantime.
2. Call `plan.manage` with `operation` create and the `goal`, written out in full with everything the person said. Do not write the steps yourself: a planner drafts them, gives every step an owner (you, the person, or you once the person gives you something), a day, and writes down its assumptions.
3. Answer with the plan in a few short lines (the phases, the first step, the total time and cost you expect) and ask the person to tap Start. Do not start any step yourself.
4. A bill of materials is part of any project that needs things bought. Make it as a spreadsheet with `document.manage` (a table: item, spec, quantity, estimated price, two or three places to buy with their links, and a status: to buy, ordered, arrived), saved in the project's folder if it has one, and say where it is. Check prices with a quick search; never state a price you did not see. Group what can be bought together.
5. Buying is the person's yes, always: prepare the order (the "Buy something" skill) and ask. When they say they bought something, update the plan and the sheet and write one line in the notebook with what, from where, for how much.
6. Safety: for anything electrical, structural, with a pool or excavation, a pressure vessel, a battery pack or a motor that can hurt someone, say plainly what needs a licensed person or a permit where they live, cite where you found it, and do not improvise.
7. Walk the project with them. When they come back ("where were we?", "what is next?", "I finished the frame"), look at the plan and the notebook, say the next step and why, and update the plan as the facts change: `revise` (with their words as the instruction) when they want the plan itself changed ("add a step for permits", "I do not see where the pictures come from"), `update_step`, `add_step` or `shift_dates` for one small change. A plan over three weeks gets a weekly check-in (`set_check_in` with `days` 7) without being asked. A fact they give that later steps need (the plot, the budget, a choice) goes in with `add_note`, once, in their words.
8. Keep decisions and reasons: when they choose something ("fiberglass, not concrete"), the notebook gets one line saying what was chosen and why, so months later "what did we decide about the pool?" has an answer.
9. Money is part of the walk. When they ask "what have I spent?" or "what is left to buy?", read the project's page (`memory.manage` with `project` and the project's name) and the bill of materials, and add the amounts up with a program, never in your head; list what was bought, from where and for how much, then what is still to buy. A purchase they tell you about ("I bought the pump for 180 dollars") is one notebook line with the item, the amount and the seller, and the sheet's status changes to ordered or arrived.
