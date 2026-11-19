"""User preference for routine decisions, separate from capability authorization (orchestration/autonomy.py has the levels)."""

from agent_control.orchestration.autonomy import AUTONOMOUS, BALANCED, autonomy_of

_GUIDANCE = {
    1: "Ask before meaningful implementation choices, while avoiding repeated questions.",
    3: (
        "Do not ask the person. Work out what is meant from their words and from what you know about them, choose the tools, methods and details yourself, "
        "try reasonable alternatives when something fails, and say in your first answer what you assumed so a wrong guess can be corrected in a word. "
        "Only something nobody but the person can give (a code sent to their phone, a legal consent) is worth a question. "
        "Something this assistant has no way to do (placing a call, anything only a person can do) is not a routine choice to find a way around: "
        "say plainly that it cannot be done and offer the nearest thing that can. Never buy anything or enter a payment: prepare it up to the payment page and ask for a yes to that exact order."
    ),
}


def initiative_guidance(level: object) -> str:
    """A preference for model judgment; it never widens policy or overrides the user."""
    if isinstance(level, bool) or not isinstance(level, int):
        return ""
    effective = autonomy_of({"initiative_level": level})
    # Preserve existing prompts and scenario recordings for the default configuration.
    if effective == BALANCED:
        return ""
    guidance = _GUIDANCE.get(effective)
    if guidance is None:
        return ""
    label = "Ask" if effective == 1 else "Autonomous"
    suffix = (
        "Follow explicit task instructions. This preference does not grant access, authorize external agents or override tool policy."
        if effective < AUTONOMOUS
        else "Follow explicit task instructions. Routine approvals are granted by the assistant itself at this level; payments and anything unrecoverable still wait for the person. "
        "This preference does not grant access, authorize external agents or override tool policy."
    )
    return f"\n\nDecision preference ({label}, level {effective} of 3): {guidance} {suffix}"
