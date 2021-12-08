from copy import deepcopy
import sys
import tempfile

import pdf2image
import pyglet
import tqdm


def bestmode(screen):
    return max(screen.get_modes(), key=lambda m: m.height)


REPEAT_TRIGGER = 0.4
REPEAT_INTERVAL = 0.1

UP = 0
HOLD = 1
FIRE = 2

class Repeater:
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
        self.rev = Repeater()
        self.fwd = Repeater()
        self.cursor = 0
        self.nslides = nslides

    def tick(self, dt, reverse, forward):
        # TODO: Make sure this is the right thing to do when both are held.
        if reverse and forward:
            return
        self.cursor -= self.rev.tick(dt, reverse)
        self.cursor += self.fwd.tick(dt, forward)
        self.cursor = min(self.cursor, self.nslides - 1)
        self.cursor = max(self.cursor, 0)


def main():

    display = pyglet.canvas.get_display()
    screens = display.get_screens()
    modes = [bestmode(s) for s in screens]

    if len(screens) == 1:
        win_audience = pyglet.window.Window(caption="audience", width=1400, height=800)
        win_presenter = pyglet.window.Window(caption="presenter")
    elif len(screens) == 2:
        idx_macbook = [i for i, mode in modes if mode.height == 900]
        if len(idx_macbook) == 0:
            raise RuntimeError("MacBook not found.")
        idx_macbook = idx_macbook[0]
        win_presenter = pyglet.window.Window(
            caption="presenter",
            screen=screens[idx_macbook],
        )
        win_audience = pyglet.window.Window(
            fullscreen=True,
            screen=screens[1 - idx_macbook],
        )
    else:
        raise RuntimeError("Don't know what to do with more than 2 screens!")

    _, rasterize_height = win_audience.get_size()
    print(f"rasterizing to h={rasterize_height}...")

    path = sys.argv[1]
    with tempfile.TemporaryDirectory() as tempdir:
        paths = pdf2image.convert_from_path(
            path,
            size=(None, rasterize_height),
            output_folder=tempdir,
            paths_only=True,
            thread_count=4,
        )
        paths = paths[:15]
        imgs = [pyglet.image.load(path) for path in tqdm.tqdm(paths)]

    print("...done rasterizing.")
    cursor = Cursor(len(paths))
    remote_fwd = False
    remote_rev = False

    @win_audience.event
    def on_draw():
        # print("audience draw")
        win_audience.clear()
        imgs[cursor.cursor].blit(0, 0)

    @win_presenter.event
    def on_draw():
        # print("presenter draw")
        win_presenter.clear()
        if cursor.cursor + 1 < cursor.nslides:
            imgs[cursor.cursor + 1].blit(0, 0)

    """
    @win_presenter.event
    def on_key_press(symbol, modifiers):
        nonlocal cursor
        if symbol == pyglet.window.key.RIGHT:
            cursor = min(nslides - 1, cursor + step)
        elif symbol == pyglet.window.key.LEFT:
            cursor = max(0, cursor - step)
        win_audience.dispatch_event("on_draw")
        return pyglet.event.EVENT_HANDLED

    @win_presenter.event
    def on_mouse_press(x, y, button, modifiers):
        print("mouse", (x, y), button, modifiers)

    @win_presenter.event
    def on_mouse_scroll(x, y, scroll_x, scroll_y):
        print("scroll", (x, y), scroll_x, scroll_y)
    """

    def on_remote_fwd(value):
        nonlocal remote_fwd
        remote_fwd = value
        return pyglet.event.EVENT_HANDLED

    def on_remote_rev(value):
        nonlocal remote_rev
        remote_rev = value
        return pyglet.event.EVENT_HANDLED

    def on_tick(dt, keyboard):
        nonlocal cursor
        forward = remote_fwd or keyboard[pyglet.window.key.RIGHT]
        reverse = remote_rev or keyboard[pyglet.window.key.LEFT]
        cursor.tick(dt, reverse, forward)
        win_audience.dispatch_event("on_draw")
        win_presenter.dispatch_event("on_draw")

    devices = pyglet.input.get_devices()
    remotes = [d for d in devices if d.name == "USB Receiver"]
    # TODO: Document / justify.
    for i, r in enumerate(remotes):
        r.open()
        for c in r.get_controls():
            if c.raw_name == "0x7:4b":
                c.on_change = on_remote_rev
            if c.raw_name == "0x7:4e":
                c.on_change = on_remote_fwd

    keyboard = pyglet.window.key.KeyStateHandler()
    win_presenter.push_handlers(keyboard)
    pyglet.clock.schedule_interval(on_tick, 0.05, keyboard=keyboard)

    # Main loop.
    win_presenter.activate()
    pyglet.app.run()



if __name__ == "__main__":
    main()
