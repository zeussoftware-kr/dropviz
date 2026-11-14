#!/usr/bin/env python3
"""Write the DropViz logo files and thumbnails into docs/brand/ (SVG by hand, PNG/ICO through a headless browser).
The mark is a treemap: one square divided into tiles of different sizes, two of them in the done green.
Needs Playwright (and Pillow for favicon.ico). Usage: python3 examples/make_brand.py"""
import base64
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "docs", "brand")
STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"
CSS_STACK = "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica Neue,Arial,sans-serif"  # unquoted: safe inside a style attribute
DARK = {"bg": "#161a21", "edge": "#2c333f", "done": "#3fd16f", "teal": "#3f9fb5", "grey_a": "#5b6573", "grey_b": "#3b4250", "text": "#f2f4f7"}
LIGHT = {"bg": "#ffffff", "edge": "#d5dbe3", "done": "#2bb55a", "teal": "#2a8aa5", "grey_a": "#9aa5b1", "grey_b": "#c6cdd6", "text": "#161a21"}


def mark(c, size=64, x=0, y=0):
    """The square mark on a 64 x 64 grid."""
    s = size / 64.0
    r = lambda a, b, w, h, col: '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" rx="%.2f" fill="%s"/>' % (x + a * s, y + b * s, w * s, h * s, 1.6 * s, col)
    return ('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" rx="%.2f" fill="%s" stroke="%s" stroke-width="%.2f"/>' % (
        x + 0.5 * s, y + 0.5 * s, 63 * s, 63 * s, 9 * s, c["bg"], c["edge"], 1 * s)
        + r(7, 7, 29, 50, c["done"]) + r(38, 7, 19, 23, c["teal"]) + r(38, 32, 9, 25, c["grey_a"]) + r(49, 32, 8, 11, c["done"]) + r(49, 46, 8, 11, c["grey_b"]))


def svg_mark(c):
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" role="img" aria-label="DropViz">%s</svg>\n' % mark(c)


def svg_logo(c):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 270 64" width="270" height="64" role="img" aria-label="DropViz">%s'
            '<text x="80" y="44" font-family="%s" font-size="34" font-weight="700" letter-spacing="-0.5" fill="%s">DropViz</text></svg>\n'
            % (mark(c), STACK.replace('"', "'"), c["text"]))


def write(name, text):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(text)


def main():
    from playwright.sync_api import sync_playwright
    os.makedirs(OUT, exist_ok=True)
    write("logo-mark.svg", svg_mark(DARK))
    write("logo-mark-light.svg", svg_mark(LIGHT))
    write("logo.svg", svg_logo(DARK))          # for dark backgrounds: light text
    write("logo-light.svg", svg_logo(LIGHT))   # for light backgrounds: dark text
    map_png = os.path.join(os.path.dirname(OUT), "screenshots", "map.png")
    with open(map_png, "rb") as fh:
        shot = base64.b64encode(fh.read()).decode()
    pitch = "See what your AI coding agents actually finished: verified, merged, per feature."

    def card(w, h):
        return ("<html><body style='margin:0;width:%dpx;height:%dpx;background:linear-gradient(135deg,#0f1218,#1b2230);position:relative;overflow:hidden;"
                "font-family:%s'>" % (w, h, CSS_STACK)
                + "<div style='position:absolute;left:%dpx;top:%dpx;display:flex;align-items:center;gap:22px'>" % (int(w * 0.055), int(h * 0.12))
                + "<svg width='%d' height='%d' viewBox='0 0 64 64'>%s</svg>" % (int(h * 0.17), int(h * 0.17), mark(DARK))
                + "<span style='color:#f2f4f7;font-size:%dpx;font-weight:700;letter-spacing:-1px'>DropViz</span></div>" % int(h * 0.13)
                + "<div style='position:absolute;left:%dpx;top:%dpx;width:%dpx;color:#e3e7ee;font-size:%dpx;line-height:1.28;font-weight:600'>%s</div>"
                % (int(w * 0.055), int(h * 0.37), int(w * 0.42), int(h * 0.072), pitch)
                + "<div style='position:absolute;left:%dpx;top:%dpx;width:%dpx;color:#8d97a6;font-size:%dpx;line-height:1.35'>Verified by git, not by what an assistant says. Runs on your computer.</div>"
                % (int(w * 0.055), int(h * 0.80), int(w * 0.42), int(h * 0.036))
                + "<div style='position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx;border-radius:14px;overflow:hidden;box-shadow:0 20px 60px #000c;border:1px solid #2c333f'>"
                % (int(w * 0.52), int(h * 0.10), int(w * 0.50), int(h * 0.80))
                + "<img src='data:image/png;base64,%s' style='width:%dpx;margin:-30px 0 0 -10px'></div></body></html>" % (shot, int(w * 0.80)))

    with sync_playwright() as p:
        b = p.chromium.launch()
        for name, w, h in (("social-preview.png", 1280, 640), ("og-image.png", 1200, 630)):
            pg = b.new_page(viewport={"width": w, "height": h})
            pg.set_content(card(w, h))
            pg.wait_for_timeout(500)
            pg.screenshot(path=os.path.join(OUT, name))
            pg.close()
        for size, name in ((16, "favicon-16.png"), (32, "favicon-32.png"), (180, "apple-touch-icon.png")):
            pg = b.new_page(viewport={"width": size, "height": size})
            pg.set_content("<html><body style='margin:0;background:transparent'><svg width='%d' height='%d' viewBox='0 0 64 64'>%s</svg></body></html>"
                           % (size, size, mark(DARK)))
            pg.screenshot(path=os.path.join(OUT, name), omit_background=True)
            pg.close()
        pg = b.new_page(viewport={"width": 1200, "height": 380})
        pg.set_content("<html><body style='margin:0;display:flex;gap:40px;padding:30px;background:#0f1218'>"
                       "<div style='background:#0f1218;padding:20px'>%s</div><div style='background:#fff;padding:20px'>%s</div></body></html>"
                       % (svg_logo(DARK), svg_logo(LIGHT)))
        pg.close()
        b.close()
    try:
        from PIL import Image
        im = Image.open(os.path.join(OUT, "favicon-32.png")).convert("RGBA")
        im.save(os.path.join(OUT, "favicon.ico"), sizes=[(16, 16), (32, 32)])
    except ImportError:
        pass
    for f in sorted(os.listdir(OUT)):
        print("wrote", os.path.join(OUT, f))


if __name__ == "__main__":
    main()
