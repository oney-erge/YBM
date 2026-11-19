---
name: Find them all
description: find every one of something in a place and compare them, for as long as it takes, checking each against criteria with a source for every value
when: Does the request ask for ALL or EVERY one of some kind of local place, business or provider in an area to be found and compared? A single answer or a topic to research is not that.
when_at: 0.7
route: job
tools:
  - job.manage
---
Use this for "find all the daycares around me and compare them", "find the best pastel de nata within 40 miles, take your time", "list every pool builder in the county with their licenses". It is a long job, hours or days, and it runs by itself. A request for one answer ("what is the best daycare") is not this: answer that now.

1. The first step is made for you: a survey is started with what the person said. If it was not, call `job.manage` with operation `start_survey` and a goal that keeps the place, what they care about and anything they said about time.
2. Do not ask the person what to compare or how long: the survey works that out from their words and says its assumptions in its first message. It keeps going until three rounds of new searches in a row find almost nothing new and the table is nearly full, or three days pass.
3. Tell the person in two sentences what it is doing and about how long, that it will message them as it goes and send a summary, and that they can ask how far it is, pause it or stop it.
