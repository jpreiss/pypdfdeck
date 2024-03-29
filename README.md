# pypdfdeck

This is a tool for displaying a PDF file (e.g. from LaTeX Beamer) as a slide show.

The main features are:
- Separate presenter window showing one slide ahead.
- Countdown timer.
- Slide dissolve animations (constant time, all slides).
- Rasterizes pages before showing them, so the slide is not briefly blurred like Preview on a Mac.
- Responds to Page Up/Down keys for compatibility with remote controls.

## Usage

```
python3 pdfdeck.py ~/path/to/my_slides.pdf
```

## Design

We use the [`pdf2image`](https://github.com/Belval/pdf2image) library to rasterize PDFs.
`pdf2image` is a Python wrapper around the [`Poppler`](https://poppler.freedesktop.org/) binaries.

We use [`pyglet`](https://pyglet.org/) to draw graphics, interact with the window system, read the keyboard/mouse, and to load and play videos.
Depending on the platform, `pyglet` might also require `ffmpeg` for video functionality (experimental, on branch `video` for now).

## Related work

There are many similar projects:

- [dspdfviewer](http://dspdfviewer.danny-edel.de)
- [Impressive](http://impressive.sourceforge.net)
- [GL-Presenter](https://www.unix-ag.uni-kl.de/~kldenker/gl_presenter)
- [PDF Presenter](http://pdfpresenter.sourceforge.net)
- [pdfpc](https://pdfpc.github.io)
- [pympress](https://cimbali.github.io/pympress/README.html)
- [QPdfPresenter](https://sourceforge.net/projects/qpdfpresenter)
- [Slider](https://github.com/TrilbyWhite/Slider)

but none of them combines all of the features I wanted:

- Supports Linux, MacOS, and Windows
- Interacts with the OS via a lightweight event/GPU-focused library instead of a full cross-platform GUI toolkit.
- Is written in scripting language. (PDF rasterization is the only performance-critical operation, and it's done with a library, so a fast language is not necessary.)
- Supports multiple monitors and a presenter view.
