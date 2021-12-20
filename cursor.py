"""Implements cursor logic, including key repeats and dissolve timing."""

REPEAT_TRIGGER = 0.4
REPEAT_INTERVAL = 0.1
DISSOLVE_TIME = 0.35

UP = 0
HOLD = 1
FIRE = 2


class _Repeater:
    """Implements repeat-after-hold, similar to OS keyboard repeating."""
    def __init__(self):
        self.state = UP
        # TODO: should never read uninitialized...
        self.stopwatch = -1000000000000

    def tick(self, dt, is_down):
        """Processes one time interval and returns the number of repeats fired.

        Args:
            dt: Time interval in seconds.
            is_down: State of the key/button during interval.

        Returns: The number of repeats fired during the interval.
        """
        if not is_down:
            self.state = UP
            return 0
        # Key is down.
        if self.state == UP:
            self.state = HOLD
            self.stopwatch = dt
            # Rising edge fire.
            return 1
        elif self.state == HOLD:
            self.stopwatch += dt
            if self.stopwatch < REPEAT_TRIGGER:
                return 0
            else:
                self.state = FIRE
                self.stopwatch -= REPEAT_TRIGGER
                return 1 + self._countdown()
        elif self.state == FIRE:
            self.stopwatch += dt
            return self._countdown()

    def _countdown(self):
        fires = 0
        while self.stopwatch > REPEAT_INTERVAL:
            fires += 1
            self.stopwatch -= REPEAT_INTERVAL
        return fires


class Cursor:
    """Implements cursor logic."""
    def __init__(self, nslides):
        self.rev = _Repeater()
        self.fwd = _Repeater()
        self.cursor = 0
        self.prev_cursor = 0
        self.nslides = nslides
        self.time_since_change = 0.0

    def tick(self, dt, reverse, forward):
        """Returns True if a redraw might be necessary."""
        old_value = self.cursor
        # Avoid oscillations when holding both keys.
        if not (reverse and forward):
            self.cursor -= self.rev.tick(dt, reverse)
            self.cursor += self.fwd.tick(dt, forward)
            self.cursor = min(self.cursor, self.nslides - 1)
            self.cursor = max(self.cursor, 0)
        if self.cursor != old_value:
            self.prev_cursor = old_value
            self.time_since_change = 0.0
        else:
            self.time_since_change += dt
        return (
            self.time_since_change < DISSOLVE_TIME or
            self.rev.state in (HOLD, FIRE) or
            self.fwd.state in (HOLD, FIRE)
        )

    def blend(self):
        return min(DISSOLVE_TIME, self.time_since_change) / DISSOLVE_TIME
