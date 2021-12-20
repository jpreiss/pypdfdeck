import sys

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


def PIL2pyglet(image):
    """Converts a PIL image from _rasterize_worker into a Pyglet image."""
    raw = image.tobytes()
    # Returns ImageData instead of Texture so we lazily load slides onto GPU.
    image = pyglet.image.ImageData(
        image.width, image.height, "RGB", raw, pitch=-image.width * 3)
    return image


class Window:
    def __init__(self, name, pdfpath, cursor, offset):
        self.name = name
        self.cursor = cursor
        self.offset = offset
        self.rasterizer = ThreadedRasterizer(pdfpath, pagelimit=cursor.nslides)
        self.textures = [None] * cursor.nslides
        self.window = pyglet.window.Window(caption=name, resizable=True)
        self.window.set_handler("on_resize", self.on_resize)
        self.window.set_handler("on_draw", self.on_draw)

    def on_resize(self, width, height):
        self.rasterizer.push_resize(width, height)

    def on_draw(self):
        self.window.clear()

        index = self.cursor.cursor + self.offset
        if index < 0 or index >= self.cursor.nslides:
            return pyglet.event.EVENT_HANDLED

        image = self.rasterizer.get(index)
        if image is None:
            return pyglet.event.EVENT_HANDLED

        tex = self.textures[index]
        if tex is None or (tex.width, tex.height) != image.size:
            tex = PIL2pyglet(image).get_texture()
            self.textures[index] = tex

        dx = (self.window.width - tex.width) // 2
        dy = (self.window.height - tex.height) // 2
        tex.blit(dx, dy)
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
