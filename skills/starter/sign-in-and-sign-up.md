---
name: Sign in and sign up for a site
description: sign in to a website with the saved login, or make an account there when there is none, choosing the username and keeping the password in the vault
when: Is the user asking you to sign in to, log in to, register at, create an account on or set up a login for a website or service?
when_at: 0.6
tools:
  - browser.open
  - browser.control
  - computer.use
  - artifact.deliver
---
Do this yourself. Do not ask the person to sign in or to give you a password: the vault keeps the logins and the page fields take placeholders, so you never need to see or type a secret.

1. Look in the vault first. If a login for the site is saved, sign in with `{{saved_username}}` and `{{saved_password}}` in the form fields. If the browser profile you drive is already signed in to the site, there is nothing to do.
2. If there is none, make the account. Open the sign-up page, choose a username (the person's own if they said one, otherwise an address of the assistant's own mailbox if it has one, otherwise a plain name), and put `{{new_password}}` in the password and the confirm-password fields. The vault makes the password and keeps it with the username under the site's host.
3. Free is free: do not accept a trial, a plan or a form that asks for a card. That is a purchase and waits for a yes (see "Buy something").
4. A puzzle (CAPTCHA), a text-message code or an email link exist to stop automated sign-ups. For an email link, read the assistant's own mailbox with `mail.manage` and follow it. For a puzzle or a text code, try another route first (another provider, a signed-in session). Only when none is left, send one message with a screenshot and the one step needed.
5. When it works, write what you learned into a skill for the site (how the form is laid out, where the button is, what the confirmation looks like), so next time it is a script, then tell the person the username and the password in one message. The message is deleted after a day; the console's Accounts page keeps the record.
6. Never reuse a password between sites, and never put one in a file, a log or a note.
