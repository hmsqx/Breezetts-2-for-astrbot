"""Choose voice-message boundaries only at detected vocal pauses."""

import math
from numbers import Real


def _times(values, duration):
    """Validate, sort and deduplicate timestamps, excluding the clip edges."""
    if not isinstance(values, (list, tuple)):
        raise ValueError("Timestamps must be a list of finite numbers")  # noqa: TRY004
    result = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("Timestamps must be finite numbers")  # noqa: TRY004
        number = float(value)
        if not math.isfinite(number) or not 0 <= number <= duration:
            raise ValueError("Timestamp is outside the audio duration")
        if 0.3 < number < duration - 0.3:
            result.append(number)
    ordered = []
    for number in sorted(result):
        if not ordered or number - ordered[-1] >= 0.05:
            ordered.append(number)
    return ordered


def choose_voice_cuts(
    duration: float, pauses: list[float], phrase_hints: list[float] | None = None
) -> list[float]:
    """Choose safe cuts, or return the full interval when none are feasible.

    Args:
        duration: Total clip duration in seconds.
        pauses: Candidate vocal pauses relative to the clip start.
        phrase_hints: Optional lyric phrase starts relative to the clip start.

    Returns:
        Sorted boundaries including 0 and duration. When no safe segmentation
        exists, returns [0, duration]; the caller must reject any interval over
        60 seconds before sending voice messages.

    Raises:
        ValueError: A duration or timestamp is invalid or outside the clip.
    """
    if isinstance(duration, bool) or not isinstance(duration, Real):
        raise ValueError("Duration must be a positive finite number")  # noqa: TRY004
    duration = float(duration)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Duration must be a positive finite number")
    points = _times(pauses, duration)
    hints = _times(phrase_hints if phrase_hints is not None else [], duration)
    preferred = set()
    for hint in hints:
        if points:
            point = min(points, key=lambda value: (abs(value - hint), value))
            if abs(point - hint) <= 1.5:
                preferred.add(point)

    if duration < 4:
        return [0.0, duration]

    boundaries = [0.0, *points, duration]
    costs = [math.inf] * len(boundaries)
    previous = [None] * len(boundaries)
    costs[0] = 0.0
    for end_index in range(1, len(boundaries)):
        end = boundaries[end_index]
        for start_index in range(end_index):
            length = end - boundaries[start_index]
            if length <= 0 or length > 60 or not math.isfinite(costs[start_index]):
                continue
            # Keep 20–45 seconds as a preference, never as a hard boundary.
            penalty = 2 * (max(0.0, 20 - length) / 20) ** 2
            penalty += 2 * (max(0.0, length - 45) / 15) ** 2
            if end_index < len(boundaries) - 1:
                penalty += 0.8 - (2.0 if end in preferred else 0.0)
            cost = costs[start_index] + penalty
            if cost < costs[end_index]:
                costs[end_index] = cost
                previous[end_index] = start_index

    if previous[-1] is None:
        return [0.0, duration]
    cuts = []
    index = len(boundaries) - 1
    while index is not None:
        cuts.append(boundaries[index])
        index = previous[index]
    return list(reversed(cuts))
