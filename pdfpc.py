import multiprocessing
import sys
import tempfile
import threading

import pdf2image
import pyglet


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
    """Shared state for communicating with rasterize_worker thread."""
    def __init__(self, path, pagelimit=None):
        self.images = None
        self.textures = [None] * pagelimit
        self.window_size = None

        self.queue = multiprocessing.Queue()
        self.thread = threading.Thread(
            target=rasterize_worker,
            args=(path, pagelimit, self.queue, self.images_done),
        )
        self.lock = threading.Lock()

        self.thread.start()

    def images_done(self, images, window_size):
        # Defer converting PIL to Pyglet to the GUI thread, otherwise weird
        # things happen with Pyglet deleting textures that are still in use.
        with self.lock:
            self.images = images
            self.window_size = window_size

    def push_resize(self, width, height):
        self.queue.put((width, height))

    def draw(self, cursor):
        with self.lock:
            if self.images is None:
                return
            w, h = self.window_size
            image = self.images[cursor]
        tex = self.textures[cursor]
        if tex is None or (tex.width, tex.height) != (w, h):
            tex = PIL2pyglet(image).get_texture()
            self.textures[cursor] = tex
        dx = (w - tex.width) // 2
        dy = (h - tex.height) // 2
        # TODO: Get rid of 1-pixel slop.
        assert (dx <= 1) or (dy <= 1)
        tex.blit(dx, dy)


def parse_aspect_from_pdfinfo(info):
    size_str = info["Page size"]
    width, _, height, _ = size_str.split(" ")
    return float(width) / float(height)


class Window:
    def __init__(self, name, pdfpath, cursor, offset):
        self.name = name
        self.cursor = cursor
        self.offset = offset
        self.rasterizer = ThreadedRasterizer(pdfpath, pagelimit=cursor.nslides)
        self.window = pyglet.window.Window(caption=name, resizable=True)
        self.window.set_handler("on_resize", self.on_resize)
        self.window.set_handler("on_draw", self.on_draw)

    def on_resize(self, width, height):
        self.rasterizer.push_resize(width, height)

    def on_draw(self):
        self.window.clear()
        index = self.cursor.cursor + self.offset
        if index >= 0 and index < self.cursor.nslides:
            self.rasterizer.draw(index)
        return pyglet.event.EVENT_HANDLED


def main():

    display = pyglet.canvas.get_display()
    screens = display.get_screens()
    # TODO: Uncomment when implementing fullscreen.
    # modes = [bestmode(s) for s in screens]

    path = sys.argv[1]
    info = pdf2image.pdfinfo_from_path(path)
    npages = info["Pages"]
    npages = min(npages, 5)

    cursor = Cursor(npages)
    presenter = Window("presenter", path, cursor, offset=1)
    audience = Window("audience", path, cursor, offset=0)

    def on_tick(dt, keyboard):
        nonlocal cursor
        forward = any(keyboard[k] for k in KEYS_FWD)
        reverse = any(keyboard[k] for k in KEYS_REV)
        if cursor.tick(dt, reverse, forward):
            presenter.window.dispatch_event("on_draw")
            audience.window.dispatch_event("on_draw")

    keyboard = pyglet.window.key.KeyStateHandler()
    presenter.window.push_handlers(keyboard)
    audience.window.push_handlers(keyboard)
    pyglet.clock.schedule_interval(on_tick, 0.05, keyboard=keyboard)

    # Main loop.
    presenter.window.activate()
    pyglet.app.run()



if __name__ == "__main__":
    main()
