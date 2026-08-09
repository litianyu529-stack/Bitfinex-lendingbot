"""Pure fail-closed decisions shared by durable write recovery paths."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RestartTransition:
    state: str
    write_phase: str
    resolution: str
    manual_safe: bool


def restart_transition(intent_state):
    state = str(intent_state).upper()
    if state == "PLANNED":
        return RestartTransition("CLOSED", "NOT_SENT", "PROCESS_RESTART_BEFORE_SEND", False)
    if state == "SUBMITTING":
        return RestartTransition("AMBIGUOUS", "UNKNOWN", "PROCESS_RESTART_AFTER_SEND", True)
    return None


def unique_unbound_candidate(candidate_ids, already_bound=()):
    candidates = {int(value) for value in candidate_ids}
    candidates.difference_update(int(value) for value in already_bound)
    return candidates.pop() if len(candidates) == 1 else None


def can_clear_ambiguous_pause(unresolved_count, runtime_mode, _protected_manual=False):
    return int(unresolved_count) == 0 and str(runtime_mode).upper() == "PAUSED"
