# pypdfdeck

This is a tool for displaying a PDF file (e.g. from LaTeX Beamer) as a slide show.
It has similar goals as [pdfpc](https://github.com/pdfpc/pdfpc),
but is written in Python and may be easier to install on a non-Linux machine.

The main features are:
- Separate presenter window showing one slide ahead.
- Rasterizes pages before showing them, so the slide is not briefly blurred like Preview on a Mac.
- Responds to Page Up/Down keys for compatibility with remote controls.

## Usage

```
python3 pdfdeck.py ~/path/to/my_slides.pdf
```
