import argparse
from pathlib import Path
import time

import pdf2image
import pyglet

from cursor import Cursor
from rasterizer import ThreadedRasterizer


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

# Redraw unconditionally every SLOW_TICK seconds. This allows us to avoid
# making the main thread event-driven.
SLOW_TICK = 0.5
# Redraw much faster when animating a dissolve.
FAST_TICK = 1.0 / 60

COLOR_OK = (50, 100, 200, 255)
COLOR_OVERTIME = (200, 50, 50, 255)


def PIL2pyglet(image):
    """Converts a PIL image from the rasterizer into a Pyglet image."""
    raw = image.tobytes()
    image = pyglet.image.ImageData(
        image.width, image.height, "RGB", raw, pitch=-image.width * 3)
    return pyglet.sprite.Sprite(image)


def compute_image_height(doc_aspect, win_w, win_h, extras_ratio=0.0):
    """Computes the image height with optional space for extras.

    Args:
        doc_aspect: The aspect ratio of the document.
        win_w: The width of the window.
        win_h: The height of the window.
        extras_ratio: The desired ratio (extras height) / (image height).

    Returs:
        img_h: Height of the image such the image and extra vertical space of
          extras_ratio * img_h fit within the window.
    """
    win_aspect = win_w / win_h
    content_aspect = doc_aspect / (1.0 + extras_ratio)
    if win_aspect < content_aspect:
        # Tall window - pad.
        content_h = win_w / content_aspect
    else:
        # Wide window - tight fit.
        content_h = win_h
    # solve img_h + extras_ratio * img_h = content_h
    img_h = content_h / (1.0 + extras_ratio)
    return img_h


class TimerDisplay:
    """Timing code and text output for countdown timer."""
    def __init__(self, duration_secs):
        self.duration = duration_secs
        self.started = None

    def label(self, **kwargs):
        """Returns a colored text label showing the time remaining.

        Args:
            **kwargs: Forwarded to pyglet.text.Label constructor.
        """
        if self.started is None:
            # TODO: Is this definitely the best clock? We want monotonicity but
            # low drift is also important.
            self.started = time.monotonic()
        remaining = self.duration - (time.monotonic() - self.started)
        remaining_struct = time.localtime(abs(remaining))
        HOUR = 60 * 60
        if self.duration < HOUR and remaining < HOUR:
            s = time.strftime("%M:%S", remaining_struct)
        else:
            s = time.strftime("%H:%M:%S", remaining_struct)
        if remaining < 0:
            color = COLOR_OVERTIME
            s = "-" + s + " "
        else:
            color = COLOR_OK
        label = pyglet.text.Label(
            s,
            color=color,
            **kwargs,
        )
        return label


def pix2font():
    """Estimates the ratio (font size) / (height of number chars in pixels)."""
    sizes = [10 * i for i in range(1, 11)]
    ascents = [pyglet.font.load(size=s).ascent for s in sizes]
    ratios = [s / a for s, a in zip(sizes, ascents)]
    # Be conservative - padding is better than cutting off.
    return min(ratios)


PIX2FONT = pix2font()
TIMER_MARGIN_TOP_RATIO = 0.0
TIMER_RATIO = 0.17
TIMER_MARGIN_BOTTOM_RATIO = 0.04
HEIGHT_RATIOS = [1.0, TIMER_MARGIN_TOP_RATIO, TIMER_RATIO, TIMER_MARGIN_BOTTOM_RATIO]
EXTRAS_RATIO = sum(HEIGHT_RATIOS[1:])
# TODO: Add common monospace fonts to list. (Note: Sadly, Pyglet does not
# appear to support any notion of "default monospaced font".)
FONTS = ("Monaco", "Inconsolata",)


class VideoFrame:
    def __init__(self, videopath):
        self.name = videopath
        source = pyglet.media.load(videopath)
        # TODO: Is there a less verbose way to get to time 0 and paused?
        player = source.play()
        player.loop = True
        player.pause()
        player.seek(0)
        format = source.video_format
        self.aspect = float(format.width) / format.height
        self.player = player
        self.sprite = pyglet.sprite.Sprite(self.player.texture)

    def ready(self):
        return True

    def reveal(self):
        self.player.seek(0)

    def foreground(self):
        if not self.player.playing:
            self.player.play()

    def hide(self):
        self.player.pause()

    def draw(self, x, y, scale, opacity):
        self.sprite._set_texture(self.player.texture)
        scale = scale / self.player.texture.height
        self.sprite.scale = scale
        self.sprite.update(x=x, y=y)
        self.sprite.opacity = opacity
        self.sprite.draw()


class PDFFrame:
    def __init__(self, rasterizer, index):
        self.rasterizer = rasterizer
        self.index = index
        self.sprite = None

    @property
    def aspect(self):
        return self.rasterizer.aspect

    def ready(self):
        return self.rasterizer.get(self.index) is not None

    def reveal(self):
        pass

    def foreground(self):
        pass

    def hide(self):
        pass

    def draw(self, x, y, scale, opacity):
        image = self.rasterizer.get(self.index)
        if self.sprite is None or (self.sprite.width, self.sprite.height) != image.size:
            self.sprite = PIL2pyglet(image)
        # Snap to pixel-perfection.
        scale = scale / self.sprite.height
        if abs(scale - 1) < 1e-2:
            scale = 1
        self.sprite.scale = scale
        self.sprite.update(x=x, y=y)
        self.sprite.opacity = opacity
        self.sprite.draw()


class Window:
    def __init__(self, name, pdfpath, cursor, offset, timer=None, video_pagepaths={}):
        self.name = name
        self.cursor = cursor
        self.offset = offset
        self.rasterizer = ThreadedRasterizer(pdfpath, pagelimit=cursor.nslides)
        self.window = pyglet.window.Window(caption=name, resizable=True)
        self.window.set_handler("on_resize", self.on_resize)
        self.window.set_handler("on_draw", self.on_draw)
        self.window.set_handler("on_close", self.on_close)
        self.ticks = 0
        self.timer = timer
        self.frames = []
        for i in range(cursor.nslides):
            if i in video_pagepaths:
                self.frames.append(VideoFrame(video_pagepaths[i]))
            else:
                self.frames.append(PDFFrame(self.rasterizer, i))
        # Rasterizer will give us a black image for end slide.
        self.frames.append(PDFFrame(self.rasterizer, cursor.nslides))
        self.letterboxes = [
            pyglet.shapes.Rectangle(0, 0, 0, 0, color=(0, 0, 0))
            for _ in range(2)
        ]

    # Event handlers.
    def on_resize(self, width, height):
        self.img_h = height / (1 + self._timer_height_factor())
        raster_h = compute_image_height(
            self.rasterizer.aspect,
            width,
            height,
            self._timer_height_factor()
        )
        raster_w = self.rasterizer.aspect * raster_h
        self.rasterizer.push_resize(raster_w, raster_h)

    def on_draw(self):
        self.ticks += 1
        self.window.clear()
        indices = (
            self.cursor.prev_cursor + self.offset,
            self.cursor.cursor + self.offset,
        )

        if not all(self.frames[i].ready() for i in indices):
            self._draw_loading()
            return pyglet.event.EVENT_HANDLED

        if self.timer is not None:
            heights = [r * self.img_h for r in HEIGHT_RATIOS]
            fontsize = PIX2FONT * heights[2]
            content_height = sum(heights)
            pad = (self.window.height - content_height) / 2
            label = self.timer.label(
                font_name=FONTS,
                font_size=fontsize,
                x=self.window.width//2,
                y=pad+heights[-1],
                anchor_x="center",
                anchor_y="baseline",
            )
            # Defer drawing until end, so it's on top of letterboxes.
            y0 = sum(heights[1:])
        else:
            y0 = 0

        frames = [self.frames[i] for i in indices]
        box_w = self.window.width
        box_h = self.img_h

        # Layout calculations. The letterbox placement for frames[0] will be
        # overwritten by those for frames[1], but it keeps the code simple.
        scales = [None, None]
        positions = [None, None]
        for i in range(2):
            scale_h = box_h
            scale_w = box_w / frames[i].aspect
            if scale_h < scale_w:
                # Height-limited.
                scales[i] = scale_h
                img_w = scale_h * frames[i].aspect
                positions[i] = (int((box_w - img_w) / 2), y0)
                for b in self.letterboxes:
                    b.height = box_h
                    b.width = positions[i][0]
                self.letterboxes[0].position = (0, 0)
                self.letterboxes[1].position = (positions[i][0] + img_w, 0)
            else:
                # Width-limited.
                scales[i] = scale_w
                positions[i] = (0, int(y0 + (box_h - scale_w) / 2))
                for b in self.letterboxes:
                    b.width = box_w
                    b.height = positions[i][1]
                self.letterboxes[0].position = (0, 0)
                self.letterboxes[1].position = (0, positions[i][1] + scale_w)

        blend = self.cursor.blend()
        if blend < 1:
            frames[0].draw(*positions[0], scales[0], opacity=0xFF)
            frames[1].reveal()
        else:
            frames[1].foreground()
            frames[0].hide()
        opacity = int(0xFF * blend)
        frames[1].draw(*positions[1], scales[1], opacity=opacity)
        for b in self.letterboxes:
            b.opacity = opacity
            b.draw()

        if self.timer is not None:
            label.draw()

        return pyglet.event.EVENT_HANDLED

    def on_close(self):
        self.rasterizer.shutdown()

    # Private methods.
    def _timer_height_factor(self):
        return EXTRAS_RATIO if self.timer is not None else 0.0

    def _draw_loading(self):
        k = self.ticks % 4
        text = "".join((" " * k, "Rasterizing", "." * k))
        label = pyglet.text.Label(
            text,
            font_name=FONTS,
            font_size=24,
            x=self.window.width//2,
            y=self.window.height//2,
            anchor_x="center",
        )
        label.draw()


def main():
    parser = argparse.ArgumentParser(description="PDF slide deck presenter.")
    parser.add_argument("path", type=str, help="PDF file path")
    parser.add_argument(
        "--pages",
        type=int,
        default=None,
        help="Limit page count (for debugging)"
    )
    parser.add_argument(
        "--countdown",
        type=float,
        default=None,
        help="Minutes for countdown timer."
    )
    args = parser.parse_args()

    info = pdf2image.pdfinfo_from_path(args.path)
    print()
    print("PDF info:")
    for k, v in info.items():
        print(f"{k}: {v}")
    print()

    npages = info["Pages"]
    if args.pages is not None:
        npages = min(npages, args.pages)

    urls = pdf2image.pdfinfo_from_path(args.path, urls=True)
    videos = {}
    if len(urls) > 0:
        root = Path(args.path).parent
        for page, type, url in urls:
            if url.startswith("file://"):
                path = Path(url[7:])
                if path.is_absolute():
                    abspath = path
                else:
                    abspath = str(root / path)
                print("found video URL", abspath)
                videos[int(page) - 1] = abspath
    print()

    cursor = Cursor(npages)
    if args.countdown is not None:
        timer = TimerDisplay(args.countdown * 60)
    else:
        timer = None
    presenter = Window("presenter", args.path, cursor, offset=1, timer=timer, video_pagepaths=videos)
    audience = Window("audience", args.path, cursor, offset=0, video_pagepaths=videos)

    def on_tick(dt, keyboard):
        nonlocal cursor
        forward = any(keyboard[k] for k in KEYS_FWD)
        reverse = any(keyboard[k] for k in KEYS_REV)
        fast = cursor.tick(dt, reverse, forward)
        if not fast:
            pyglet.clock.unschedule(on_tick)
            # Slow tick so we draw after rasterizer is done.
            pyglet.clock.schedule_interval(on_tick, SLOW_TICK, keyboard=keyboard)

    # Tick slowly except when we are updating the screen - which always begins
    # with a key press. on_tick will slow itself back down later.
    def on_key_press(window, symbol, modifiers):
        if symbol in KEYS_FWD or symbol in KEYS_REV:
            pyglet.clock.unschedule(on_tick)
            pyglet.clock.schedule_interval(on_tick, FAST_TICK, keyboard=keyboard)
            return pyglet.event.EVENT_HANDLED
        if symbol == pyglet.window.key.ESCAPE:
            # Intercept -- I am used to Escape meaning "exit fullscreen", but
            # Pyglet always uses it to close the window.
            return pyglet.event.EVENT_HANDLED

    # This cannot be a loop over [presenter, audience] due to lexical scoping.
    presenter.window.set_handler(
        "on_key_press", lambda sym, mod: on_key_press(presenter, sym, mod))
    audience.window.set_handler(
        "on_key_press", lambda sym, mod: on_key_press(audience, sym, mod))

    keyboard = pyglet.window.key.KeyStateHandler()
    presenter.window.push_handlers(keyboard)
    audience.window.push_handlers(keyboard)
    pyglet.clock.schedule_interval(on_tick, SLOW_TICK, keyboard=keyboard)

    # Main loop.
    presenter.window.activate()
    pyglet.app.run()


if __name__ == "__main__":
    main()
