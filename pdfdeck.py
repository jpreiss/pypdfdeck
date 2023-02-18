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

# Text colors for the timer in the presenter view.
COLOR_OK = (50, 100, 200, 255)
COLOR_OVERTIME = (200, 50, 50, 255)


def PIL2pyglet(image):
    """Converts a PIL image from the rasterizer into a Pyglet image."""
    raw = image.tobytes()
    image = pyglet.image.ImageData(
        image.width, image.height, "RGB", raw, pitch=-image.width * 3)
    return pyglet.sprite.Sprite(image)


class TimerDisplay:
    """Timing code and text output for countdown timer."""
    def __init__(self, duration_secs):
        self.duration = duration_secs
        self.started = None
        self._label = pyglet.text.Label(
            "",
            font_name=FONTS,
            anchor_x="center",
            anchor_y="baseline",
        )

    def label(self, fontsize, x, y):
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
        self._label.font_size = fontsize
        self._label.x = x
        self._label.y = y
        self._label.text = s
        self._label.color = color
        return self._label


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


def boxfill_centered(w, h, box_w, box_h):
    """Coordinates and scale to make box(w, h) fit inside box(box_w, box_h)."""
    scale_h = box_h / h
    scale_w = box_w / w
    if scale_h < scale_w:
        # Height-limited.
        return (box_w - scale_h * w) / 2, 0, scale_h
    else:
        # Width-limited.
        return 0, (box_h - scale_w * h) / 2, scale_w


class VideoFrame:
    """Polymorphic class for video player frame."""
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
    """Polymorphic class for rasterized PDF page frame."""
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
        self.loading_label = pyglet.text.Label(
            "",
            font_name=FONTS,
            font_size=24,
            anchor_x="center",
        )

    def on_resize(self, width, height):
        if self.timer is not None:
            self.img_h = int(height / (1 + EXTRAS_RATIO))
        else:
            self.img_h = height
        _, _, scale = boxfill_centered(self.rasterizer.aspect, 1, width, self.img_h)
        self.rasterizer.push_resize(int(scale * self.rasterizer.aspect), int(scale))

    def on_draw(self):
        self.ticks += 1
        self.window.clear()
        indices = (
            self.cursor.prev_cursor + self.offset,
            self.cursor.cursor + self.offset,
        )
        frames = [self.frames[i] for i in indices]

        # Draw "spinner" and bail out early if there's nothing to draw.
        if not all(f.ready() for f in frames):
            k = self.ticks % 4
            self.loading_label.text = "".join((" " * k, "Rasterizing", "." * k))
            self.loading_label.x = self.window.width // 2
            self.loading_label.y = self.window.height // 2
            self.loading_label.draw()
            return pyglet.event.EVENT_HANDLED

        # Begin layout calculations.
        box_w = self.window.width
        box_h = self.img_h
        y0 = self.window.height - self.img_h

        # The letterbox placement for frames[0] will be overwritten by those
        # for frames[1], but it keeps the code simple.
        scales = [None, None]
        positions = [None, None]
        for i in range(2):
            x, y, scale = boxfill_centered(frames[i].aspect, 1, box_w, box_h)
            positions[i] = (int(x), int(y + y0))
            scales[i] = scale
            if x > 0:
                # Height-limited.
                img_w = scale * frames[i].aspect
                for b in self.letterboxes:
                    b.height = box_h
                    b.width = positions[i][0]
                self.letterboxes[0].position = (0, 0)
                self.letterboxes[1].position = (positions[i][0] + img_w, 0)
            else:
                # Width-limited.
                for b in self.letterboxes:
                    b.width = box_w
                    b.height = positions[i][1]
                self.letterboxes[0].position = (0, 0)
                self.letterboxes[1].position = (0, positions[i][1] + scale)

        # Transitions: dissolve and start/stop of videos.
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
            heights = [r * self.img_h for r in HEIGHT_RATIOS]
            fontsize = PIX2FONT * heights[2]
            content_height = sum(heights)
            pad = (self.window.height - content_height) / 2
            label = self.timer.label(
                fontsize=fontsize,
                x=self.window.width//2,
                y=pad+heights[-1],
            )
            label.draw()

        return pyglet.event.EVENT_HANDLED

    def on_close(self):
        self.rasterizer.shutdown()


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
