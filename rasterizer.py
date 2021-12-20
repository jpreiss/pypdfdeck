"""Threaded interruptible PDF rasterizer."""

import multiprocessing
import tempfile
import threading

import pdf2image


def _winsize2rasterargs(window_size, aspect):
    width, height = window_size
    window_aspect = float(width) / height
    if window_aspect >= aspect:
        width = None
    else:
        height = None
    return (width, height)


def _rasterize_worker(pdfpath, pagelimit, size_queue, callback):
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
    aspect = _parse_aspect_from_pdfinfo(info)
    # The (one-based) index of the page we are to rasterize next. If it exceeds
    # page_limit, we have no work to do.
    page = 1
    images = [None] * pagelimit
    # Block indefinitely for first size.
    window_size = size_queue.get()
    image_size = _winsize2rasterargs(window_size, aspect)
    while True:
        while not size_queue.empty():
            # Start over!
            window_size = size_queue.get()
            image_size = _winsize2rasterargs(window_size, aspect)
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


class ThreadedRasterizer:
    """Shared state for communicating with _rasterize_worker thread."""
    def __init__(self, path, pagelimit=None):
        self.images = None
        self.window_size = None

        self.queue = multiprocessing.Queue()
        self.thread = threading.Thread(
            target=_rasterize_worker,
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

    def get(self, index):
        with self.lock:
            if self.images is None:
                return None, (None, None)
            return self.images[index], self.window_size


def _parse_aspect_from_pdfinfo(info):
    size_str = info["Page size"]
    width, _, height, _ = size_str.split(" ")
    return float(width) / float(height)
