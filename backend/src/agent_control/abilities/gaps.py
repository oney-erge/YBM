"""What this assistant cannot do, and what to do about it, as records the model can find.

A request for something that cannot be done (paying for an order) used to be handled by a rule that fired on the request's words
and told the model to stop. A rule per impossibility does not scale and hides in code what the user would want to read. These are
the same facts as data: each is an ability in the UNAVAILABLE state, found by the same search as every other ability, carrying the
honest answer and the nearest thing that can be done. Adding one is adding a record here, not a branch in the worker.

A gap is never a refusal to try. Searching finds the nearest ability the assistant does have (research, a cart list, a draft), and the
guidance names it; "cannot" is said only about the part that cannot be done.
"""

from __future__ import annotations

from agent_control.abilities.model import NEED_ACCOUNT, NEED_PAYMENT, Ability, AbilityKind, AbilityState

KNOWN_GAPS: tuple[Ability, ...] = (
    Ability(
        id="gap:commerce.order",
        kind=AbilityKind.GAP,
        name="order and pay",
        summary="buy, order, book or pay for something (food, parts, tickets, a flight, a subscription)",
        group="commerce",
        state=AbilityState.UNAVAILABLE,
        provides=("order", "buy", "purchase", "pay", "book", "reserve", "checkout", "cart", "subscribe", "pizza", "food", "ticket",
                  "flight", "hotel", "amazon", "delivery", "parts"),
        confirm="Does the user want the assistant to spend money or complete a purchase, order or booking for them?",
        needs=(NEED_PAYMENT, NEED_ACCOUNT),
        guidance=(
            "This assistant has no payment method and never enters card details or presses a final buy, order or pay button. It can do "
            "everything up to that point: find the shop, menu or product, compare options, build the list or cart contents, fill in a "
            "form short of paying, and write down the phone number or link. Say plainly that it cannot place the order, offer those "
            "steps, and let the user finish the payment. If the user wants it to pay in future, the answer is a payment capability they "
            "set up and approve, not a workaround."
        ),
        source="known gaps",
    ),
    Ability(
        id="gap:physical.act",
        kind=AbilityKind.GAP,
        name="act in the physical world",
        summary="build, assemble or repair a physical thing (a robot, a drone, furniture, a circuit), or be somewhere in person",
        group="physical",
        state=AbilityState.UNAVAILABLE,
        provides=("assemble", "solder", "robot", "drone", "hardware", "circuit", "arduino", "raspberry", "motor", "sensor", "furniture",
                  "repair", "physical"),
        confirm="Does the user want the assistant to help build, assemble or repair a real-world physical object?",
        confirm_at=0.8,
        needs=(NEED_ACCOUNT, NEED_PAYMENT),
        guidance=(
            "This assistant lives on the computer and cannot touch hardware. A goal like this is still worth taking on as a project: ask "
            "what the user wants it to be, then plan it. The plan separates what the assistant does (research, a parts list, a wiring "
            "plan, the code, the ordering links) from what the user does (buy, assemble, test), and says what the assistant needs from "
            "them for each step."
        ),
        source="known gaps",
    ),
)
