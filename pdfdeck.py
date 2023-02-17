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


class VideoOverlays:
    def __init__(self, window, pagepaths):
        self.window = window
        self.players = {}
        self.page = None
        self.blit_args = None
        for page, path in pagepaths.items():
            source = pyglet.media.load(path)
            player = source.play()
            player.loop = True
            player.seek(0)
            player.pause()
            player.monkeypatch_name = path
            self.players[page] = player

    def on_resize(self):
        if self.page not in self.players:
            return
        player = self.players[self.page]
        format = player.source.video_format
        aspect = float(format.width) / format.height
        win_w = self.window.width
        win_h = self.window.height
        h = compute_image_height(aspect, win_w, win_h)
        w = aspect * h
        if abs(h - win_h) < 1e-6:
            blit_x = (win_w - w) / 2.0
            blit_y = 0
        elif abs(w - win_w) < 1e-6:
            blit_x = 0
            blit_y = (win_h - h) / 2.0
        else:
            assert False, "bad video size calculations."
        self.blit_args = (blit_x, blit_y, 0, w, h)

    def draw_if_needed(self, page):
        if page not in self.players:
            # It's important to pause non-active players, because players
            # automatically cause pyglet to raise the window's on_draw event
            # repeatedly when playing. This will result in high power
            # consumption.
            for player in self.players.values():
                player.pause()
            self.page = None
            return False

        if self.page != page:
            if self.page is not None:
                self.players[self.page].pause()
            player = self.players[page]
            print("Starting video", player.monkeypatch_name)
            player.seek(0)
            player.play()
            self.page = page
            # Compute blit arguments.
            self.on_resize()

        self.players[self.page].texture.blit(*self.blit_args)
        return True


class Window:
    def __init__(self, name, pdfpath, cursor, offset, timer=None, video_pagepaths={}):
        self.name = name
        self.cursor = cursor
        self.offset = offset
        self.rasterizer = ThreadedRasterizer(pdfpath, pagelimit=cursor.nslides)
        self.sprites = [None] * (cursor.nslides + 2)
        self.window = pyglet.window.Window(caption=name, resizable=True)
        self.window.set_handler("on_resize", self.on_resize)
        self.window.set_handler("on_draw", self.on_draw)
        self.window.set_handler("on_close", self.on_close)
        self.ticks = 0
        self.timer = timer
        self.video_overlays = VideoOverlays(self.window, video_pagepaths)

    # Event handlers.
    def on_resize(self, width, height):
        img_h = compute_image_height(
            self.rasterizer.aspect,
            width,
            height,
            self._timer_height_factor()
        )
        img_w = self.rasterizer.aspect * img_h
        self.rasterizer.push_resize(img_w, img_h)
        self.video_overlays.on_resize()

    def on_draw(self):
        self.ticks += 1
        self.window.clear()
        indices = (
            self.cursor.prev_cursor + self.offset,
            self.cursor.cursor + self.offset,
        )

        sprites = [self._get_sprite(i) for i in indices]
        if None in sprites:
            self._draw_loading()
            return pyglet.event.EVENT_HANDLED
        # TODO: Double-check that we are doing integer and floating point
        # division in the right places.
        # TODO: Move Pyglet-independent code into layout functions or class.
        dx = (self.window.width - sprites[0].width) // 2
        if self.timer is not None:
            img_h = sprites[0].height
            heights = [r * img_h for r in HEIGHT_RATIOS]
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
            label.draw()
            dy = pad + sum(heights[1:])
        else:
            dy = (self.window.height - sprites[0].height) // 2

        # TODO: use polymorphism to treat video frames and sprite frames more
        # uniformly. It should not be complex to have features like dissolve and timer
        # work seamlessly between both frame types.

        vid0 = indices[0] in self.video_overlays.players
        vid1 = indices[1] in self.video_overlays.players

        if vid0 and vid1:
            self.video_overlays.draw_if_needed(indices[1])
            return
        if vid0 and self.cursor.blend() < 1:
            self.video_overlays.draw_if_needed(indices[0])
            sprites[1].opacity = int(255 * self.cursor.blend())
            sprites[1].update(x=dx, y=dy)
            sprites[1].draw()
            return
        if vid1:
            self.video_overlays.draw_if_needed(indices[1])
            sprites[0].opacity = int(255 * (1.0 - self.cursor.blend()))
            sprites[0].update(x=dx, y=dy)
            sprites[0].draw()
            return

        assert not self.video_overlays.draw_if_needed(indices[1])
        sprites[0].opacity = 255
        sprites[1].opacity = int(255 * self.cursor.blend())
        for s in sprites:
            s.update(x=dx, y=dy)
            s.draw()
        return pyglet.event.EVENT_HANDLED

    def on_close(self):
        self.rasterizer.shutdown()

    # Private methods.
    def _timer_height_factor(self):
        return EXTRAS_RATIO if self.timer is not None else 0.0

    def _get_sprite(self, index):
        image = self.rasterizer.get(index)
        if image is None:
            return None
        sprite = self.sprites[index + 1]
        if sprite is None or (sprite.width, sprite.height) != image.size:
            sprite = PIL2pyglet(image)
            self.sprites[index + 1] = sprite
        return sprite

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
    presenter = Window("presenter", args.path, cursor, offset=1, timer=timer)
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
