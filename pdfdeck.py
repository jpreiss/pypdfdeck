import argparse

import pdf2image
import pyglet

from cursor import Cursor
from rasterizer import ThreadedRasterizer


def bestmode(screen):
    return max(screen.get_modes(), key=lambda m: m.height)


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

SLOW_TICK = 0.5
FAST_TICK = 1.0 / 60


def PIL2pyglet(image):
    """Converts a PIL image from the rasterizer into a Pyglet image."""
    raw = image.tobytes()
    image = pyglet.image.ImageData(
        image.width, image.height, "RGB", raw, pitch=-image.width * 3)
    return pyglet.sprite.Sprite(image)


class Window:
    def __init__(self, name, pdfpath, cursor, offset):
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

    def toggle_fullscreen(self):
        self.window.set_fullscreen(not self.window.fullscreen)

    def on_resize(self, width, height):
        self.rasterizer.push_resize(width, height)

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
        # Symmetric dots make centering easier.
        dots = "." * (self.ticks % 4)
        text = "".join((dots, "Rasterizing", dots))
        label = pyglet.text.Label(
            text,
            font_size=24,
            x=self.window.width//2,
            y=self.window.height//2,
            anchor_x="center",
        )
        label.draw()

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
        dx = (self.window.width - sprites[0].width) // 2
        dy = (self.window.height - sprites[0].height) // 2
        sprites[0].opacity = 255
        sprites[1].opacity = int(255 * self.cursor.blend())
        for s in sprites:
            s.update(x=dx, y=dy)
            s.draw()
        return pyglet.event.EVENT_HANDLED

    def on_close(self):
        self.rasterizer.shutdown()


def main():

    display = pyglet.canvas.get_display()
    screens = display.get_screens()
    # TODO: Uncomment when implementing fullscreen.
    # modes = [bestmode(s) for s in screens]

    parser = argparse.ArgumentParser(description="PDF slide deck presenter.")
    parser.add_argument("path", type=str, help="PDF file path")
    args = parser.parse_args()

    info = pdf2image.pdfinfo_from_path(args.path)
    npages = info["Pages"]

    cursor = Cursor(npages)
    presenter = Window("presenter", args.path, cursor, offset=1)
    audience = Window("audience", args.path, cursor, offset=0)

    def on_tick(dt, keyboard):
        nonlocal cursor
        forward = any(keyboard[k] for k in KEYS_FWD)
        reverse = any(keyboard[k] for k in KEYS_REV)
        if not cursor.tick(dt, reverse, forward):
            pyglet.clock.unschedule(on_tick)
            # Slow tick so we draw after rasterizer is done.
            pyglet.clock.schedule_interval(on_tick, SLOW_TICK, keyboard=keyboard)

    # Tick slowly except when we are updating the screen - which always begins
    # with a key press. on_tick will slow itself back down later.
    def on_key_press(window, symbol, modifiers):
        if symbol in KEYS_FWD or symbol in KEYS_REV:
            pyglet.clock.unschedule(on_tick)
            pyglet.clock.schedule_interval(on_tick, FAST_TICK, keyboard=keyboard)
        if symbol == pyglet.window.key.F:
            window.toggle_fullscreen()

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
