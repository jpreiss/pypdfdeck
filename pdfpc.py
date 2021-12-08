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
        """Returns True if the cursor changed, false otherwise."""
        old_value = self.cursor
        # TODO: Make sure this is the right thing to do when both are held.
        if reverse and forward:
            return False
        self.cursor -= self.rev.tick(dt, reverse)
        self.cursor += self.fwd.tick(dt, forward)
        self.cursor = min(self.cursor, self.nslides - 1)
        self.cursor = max(self.cursor, 0)
        return self.cursor != old_value


def rasterize(pdfpath, width, height, progressbar=True, pagelimit=None):
    with tempfile.TemporaryDirectory() as tempdir:
        paths = pdf2image.convert_from_path(
            pdfpath,
            # TODO: Letterboxing. Currently leaves empty space on right if
            # screen is wider than slide, but clips if the slide is wider!
            size=(width, height),
            output_folder=tempdir,
            # Do not bother loading as PIL images. Let Pyglet handle loading.
            # TODO: Try to keep everything in memory.
            paths_only=True,
            thread_count=4,
        )
        # TODO: Can we do this *before* calling pdf2image?
        if pagelimit is not None:
            paths = paths[:pagelimit]
        if progressbar:
            paths = tqdm.tqdm(paths)
        imgs = [pyglet.image.load(p) for p in paths]
        return imgs


class BlockingRasterizer:
    def __init__(self, path, pagelimit=None):
        self.path = path
        self.pagelimit = pagelimit
        self.images = None
        info = pdf2image.pdfinfo_from_path(path)
        self.aspect = parse_aspect_from_pdfinfo(info)
        self.window_size = None

    def push_resize(self, width, height):
        self.window_size = (width, height)
        window_aspect = float(width) / height
        if window_aspect >= self.aspect:
            width = None
        else:
            height = None
        self.imgs = rasterize(self.path, width, height, self.pagelimit)

    def draw(self, cursor):
        w, h = self.window_size
        dx = (w - self.imgs[0].width) // 2
        dy = (h - self.imgs[0].height) // 2
        assert (dx == 0) or (dy == 0)
        self.imgs[cursor].blit(dx, dy)


def parse_aspect_from_pdfinfo(info):
    size_str = info["Page size"]
    width, _, height, _ = size_str.split(" ")
    return float(width) / float(height)


def main():

    display = pyglet.canvas.get_display()
    screens = display.get_screens()
    modes = [bestmode(s) for s in screens]

    if len(screens) == 1:
        win_audience = pyglet.window.Window(
            caption="audience",
            resizable=True,
        )
        win_presenter = pyglet.window.Window(
            caption="presenter",
            resizable=True,
        )
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

    win_dims = win_audience.get_size()
    print(f"rasterizing to {win_dims}...")
    path = sys.argv[1]
    info = pdf2image.pdfinfo_from_path(path)
    npages = info["Pages"]
    npages = min(npages, 5)
    rasterizer = BlockingRasterizer(path, pagelimit=npages)

    print("...done rasterizing.")
    cursor = Cursor(npages)
    remote_fwd = False
    remote_rev = False

    @win_audience.event
    def on_resize(width, height):
        nonlocal rasterizer
        print(f"audience resize to {width}, {height}")
        rasterizer.push_resize(width, height)

    @win_audience.event
    def on_draw():
        win_audience.clear()
        rasterizer.draw(cursor.cursor)
        return pyglet.event.EVENT_HANDLED

    @win_presenter.event
    def on_draw():
        win_presenter.clear()
        if cursor.cursor + 1 < cursor.nslides:
            rasterizer.draw(cursor.cursor + 1)
        return pyglet.event.EVENT_HANDLED

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
        if cursor.tick(dt, reverse, forward):
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
