from copy import deepcopy
import multiprocessing
import sys
import tempfile
import threading

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

KEYS_FWD = [
    pyglet.window.key.RIGHT,
    pyglet.window.key.UP,
    pyglet.window.key.PAGEDOWN,
]
KEYS_REV = [
    pyglet.window.key.LEFT,
    pyglet.window.key.DOWN,
    pyglet.window.key.PAGEUP,
]

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


def winsize2rasterargs(window_size, aspect):
    width, height = window_size
    window_aspect = float(width) / height
    if window_aspect >= aspect:
        width = None
    else:
        height = None
    return (width, height)


def rasterize_worker(pdfpath, pagelimit, size_queue, callback):
    """Threaded interruptible PDF rasterizer.

    Listens on size_queue for (width, height) tuples representing window resize
    events. When an event arrives, discards any in-progress rasterization and
    starts over. Calls the callback on its own thread when the images for the
    entire PDF are complete and the size has not changed during rasterization.

    Args:
        pdfpath (str): Path of PDF file.
        pagelimit (int): Read this many pages from the file. (Mostly for
            development purposes to keep load time down.)
        size_queue (queue-like): Queue to monitor for size changes.
        callback (fn void(list of PIL images)): Function to call when the full
            PDF is ready.
    """
    info = pdf2image.pdfinfo_from_path(pdfpath)
    aspect = parse_aspect_from_pdfinfo(info)
    # The (one-based) index of the page we are to rasterize next. If it exceeds
    # page_limit, we have no work to do.
    page = 1
    images = [None] * pagelimit
    # Block indefinitely for first size.
    window_size = size_queue.get()
    image_size = winsize2rasterargs(window_size, aspect)
    while True:
        while not size_queue.empty():
            # Start over!
            window_size = size_queue.get()
            image_size = winsize2rasterargs(window_size, aspect)
            page = 1
        if page == pagelimit + 1:
            # Got through them all without changing size.
            if images[-1] is not None:
                images2 = images
                images = [None] * pagelimit
                callback(images2, window_size)
            else:
                # Already callbacked and no new resize events since.
                pass
        else:
            with tempfile.TemporaryDirectory() as tempdir:
                # TODO: Try to keep everything in memory.
                image = pdf2image.convert_from_path(
                    pdfpath,
                    size=image_size,
                    first_page=page,
                    last_page=page,
                )
                assert len(image) == 1
                images[page - 1] = image[0]
                page += 1


def PIL2pyglet(image):
    """Converts a PIL image from rasterize_worker into a Pyglet image."""
    raw = image.tobytes()
    # Returns ImageData instead of Texture so we lazily load slides onto GPU.
    image = pyglet.image.ImageData(
        image.width, image.height, "RGB", raw, pitch=-image.width * 3)
    return image


class ThreadedRasterizer:
    def __init__(self, path, pagelimit=None):
        self.path = path
        self.pagelimit = pagelimit
        self.images = None
        self.window_size = None
        self.queue = multiprocessing.Queue()
        self.thread = threading.Thread(
            target=rasterize_worker,
            args=(path, pagelimit, self.queue, self.images_done),
        )
        self.lock = threading.Lock()
        self.thread.start()
        self.active_image = None

    def images_done(self, images, window_size):
        with self.lock:
            self.images = [PIL2pyglet(img) for img in images]
            self.window_size = window_size

    def push_resize(self, width, height):
        self.queue.put((width, height))

    def draw(self, cursor):
        with self.lock:
            if self.images is None:
                return
            w, h = self.window_size
            # If we store this pyglet image reference in a local variable
            # instead of a member of self, and self.images is later overwritten
            # by the rasterizer thread, we get segfaults when trying to blit
            # the image. Possibly the local variables in this event handler do
            # not contribute to the object reference count - see
            # https://pyglet.readthedocs.io/en/latest/modules/event.html#dispatching-events.
            # TODO: Understand more completely.
            self.active_image = self.images[cursor]
        dx = (w - self.active_image.width) // 2
        dy = (h - self.active_image.height) // 2
        # TODO: Get rid of 1-pixel slop.
        assert (dx <= 1) or (dy <= 1)
        self.active_image.blit(dx, dy)


def parse_aspect_from_pdfinfo(info):
    size_str = info["Page size"]
    width, _, height, _ = size_str.split(" ")
    return float(width) / float(height)


def main():

    display = pyglet.canvas.get_display()
    screens = display.get_screens()
    modes = [bestmode(s) for s in screens]

    win_audience = pyglet.window.Window(
        caption="audience",
        resizable=True,
    )
    win_presenter = pyglet.window.Window(
        caption="presenter",
        resizable=True,
    )

    path = sys.argv[1]
    info = pdf2image.pdfinfo_from_path(path)
    npages = info["Pages"]
    npages = min(npages, 5)
    rasterizer_audience = ThreadedRasterizer(path, pagelimit=npages)
    rasterizer_presenter = ThreadedRasterizer(path, pagelimit=npages)

    cursor = Cursor(npages)

    # TODO: Figure out the fine points of pyglet event so we don't need all
    # this copy-paste code.

    @win_audience.event
    def on_resize(width, height):
        nonlocal rasterizer_audience
        print(f"audience resize to {width}, {height}")
        rasterizer_audience.push_resize(width, height)

    @win_presenter.event
    def on_resize(width, height):
        nonlocal rasterizer_presenter
        print(f"presenter resize to {width}, {height}")
        rasterizer_presenter.push_resize(width, height)

    @win_audience.event
    def on_draw():
        win_audience.clear()
        rasterizer_audience.draw(cursor.cursor)
        return pyglet.event.EVENT_HANDLED

    @win_presenter.event
    def on_draw():
        win_presenter.clear()
        if cursor.cursor + 1 < cursor.nslides:
            rasterizer_presenter.draw(cursor.cursor + 1)
        return pyglet.event.EVENT_HANDLED

    def on_tick(dt, keyboard):
        nonlocal cursor
        forward = any(keyboard[k] for k in KEYS_FWD)
        reverse = any(keyboard[k] for k in KEYS_REV)
        if cursor.tick(dt, reverse, forward):
            win_audience.dispatch_event("on_draw")
            win_presenter.dispatch_event("on_draw")

    keyboard = pyglet.window.key.KeyStateHandler()
    win_presenter.push_handlers(keyboard)
    pyglet.clock.schedule_interval(on_tick, 0.05, keyboard=keyboard)

    # Main loop.
    win_presenter.activate()
    pyglet.app.run()



if __name__ == "__main__":
    main()
