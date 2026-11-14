---
name: Repeat something on a schedule
description: set up a job that runs again and again (every Monday at 9am, daily, every 2 hours) with schedule.manage, instead of doing the job now
when: Does the user ask for something to be done again and again on a schedule (every day, every Monday, every few hours), rather than once now?
when_at: 0.8
tools:
  - schedule.manage
---
Use this when the request repeats: "every Monday at 9am send me the top 3 news stories", "remind me every 2 hours to stretch", "check the folder every night".

1. Do not do the job now, and do not build anything for it. The schedule runs the job later, as an ordinary request, with all the tools.
2. Call `schedule.manage` with operation `create`. Set `objective` to the job in the user's own words, without the when ("send me the top 3 Hacker News stories"). Set `cadence` to the days and time as the user said them: "every monday at 09:00", "every weekday at 08:30", "daily at 07:00", "every 2 hours". Leave `timezone` out.
3. Tell the user in one sentence what will run and when, using the next run time the tool gives back.
4. Do not ask how to deliver it: the result comes to this chat.
