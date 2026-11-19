---
name: Buy something
description: find, compare and choose something to buy, fill the cart, and stop before paying, then ask the person for a yes to the exact order
when: Is the user asking you to buy, order, book, reserve, pay for or subscribe to something that costs money?
when_at: 0.65
tools:
  - browser.open
  - browser.control
  - computer.use
  - artifact.deliver
  - memory.manage
---
FIRST RULE, above everything else in this skill: never complete a purchase or submit any payment without the person's yes to the exact order. Stop on the page before payment and send the order details: items, seller, total with tax and shipping, the card's last four digits as the site shows them, delivery address. A yes covers that one order. Anything that asks for a card is a purchase: a free trial, a subscription, a "reserve now, pay later". This holds at every autonomy level, even when the person says "just buy it, you pick". The code refuses a payment click without a recorded yes; do not try to go around it.

Everything before the payment is yours, and you do it without asking:

1. Work out what is wanted from the words and from what you remember about the person (sizes, brands, address, past orders). Say what you took it to mean in one line.
2. Search and compare with `browser.open`. Prefer the seller's own page over a marketplace listing with no reviews; check the price, the shipping and the return terms, and note the link of each candidate.
3. Choose one. If the person said "you pick", pick, and say why in one sentence. If two are close, name the other as the runner-up.
4. Use `browser.control` to open the chosen page, choose the options, add it to the cart and go to the last step before payment. The browser already holds the person's address and card; you never type a card number and you never store one.
5. Stop there and send one message with the order: the items with their links, the seller, the total with tax and shipping, the card's last four digits as the site shows them, the delivery address and date, and the runner-up. Ask for a plain yes.
6. On a yes, press the final button for that order only (the payment approval is the person's yes; the gate in the code checks it), then confirm what happened from the page: the order number or the confirmation text. Say it was placed only if the page says so. Keep the order number and the date in memory, so "reorder like last time" works.
7. If the price or the items changed after the yes, stop and ask again.
8. If the site blocks automation, or asks for a code sent to the person's phone, say so in one message with the single step needed.
