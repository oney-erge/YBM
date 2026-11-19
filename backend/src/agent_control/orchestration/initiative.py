"""User preference for routine decisions, separate from capability authorization."""

_GUIDANCE = {
    1: "Ask before meaningful implementation choices, while avoiding repeated questions.",
    2: "Choose minor details yourself; ask about substantial changes of method.",
    3: "Choose routine details yourself; ask when an important preference or fact is missing.",
    4: "Choose routine tools and methods and try reasonable alternatives within the task's scope; report meaningful progress.",
    5: "Choose routine details and recovery approaches yourself; ask only for necessary information, human action, or required approval.",
}


def initiative_guidance(level: object) -> str:
    """A preference for model judgment; it never widens policy or overrides the user."""
    if not isinstance(level, int) or isinstance(level, bool) or level not in _GUIDANCE:
        return ""
    # Preserve existing prompts and scenario recordings for the default configuration.
    if level == 3:
        return ""
    return (
        f"\n\nDecision preference ({level} of 5): {_GUIDANCE[level]} "
        "Follow explicit task instructions. This preference does not grant access, approve actions, "
        "authorize external agents or override tool policy. "
        "Something this assistant has no way to do (placing a call or an order, paying, anything only a person can do) is not a routine choice to find a way around: "
        "say plainly that it cannot be done and offer the nearest thing that can."
    )
