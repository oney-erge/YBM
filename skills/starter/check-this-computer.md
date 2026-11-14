---
name: Check what this computer can run
description: answer whether this computer can run something, or how powerful it is, from its real graphics card, video memory, RAM and processor
when: Is the user asking whether this computer can run something, or how powerful this computer is (its graphics card, video memory, RAM or processor)?
when_at: 0.55
tools:
  - system.inventory
---
Use this when the answer depends on this computer's graphics card, video memory, RAM or processor: "can I run a 70B model?", "can this PC play that game?", "how powerful is my computer?", "how much RAM do I have?".

1. Call `system.inventory` with operation `hardware` first. Do not ask the user what their computer has, and do not guess: this assistant runs on that computer and can read it.
2. Say what it has in one line (the card, its video memory, the RAM).
3. Answer the question from those numbers. A model needs roughly its size in billions of parameters, in GB, at 8-bit and about half that at 4-bit, plus a few GB of working memory, and it must fit in video memory to run fast (RAM is much slower). If it does not fit, say so plainly with the numbers, and offer what does fit (a smaller model, or a 4-bit version of one) and how to get it.
4. Mention a cloud model only as something the user would have to allow.
