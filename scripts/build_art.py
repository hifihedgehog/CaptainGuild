"""Draws the shareable artwork from the logo in captainguild/web/static/img/logo.svg, so a change to the logo needs only this run again.

    python scripts/build_art.py

Needs Playwright and Microsoft Edge. Writes to assets/.
"""
from __future__ import annotations

import base64
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
LOGO = (ROOT / "captainguild/web/static/img/logo.svg").read_text(encoding="utf-8")
NAME = "Captain Guild"
TAGLINE = "Moderation, roles, logs and messages, on hardware you control"
FONTS = ROOT / "captainguild/web/static/vendor/fonts"


def font_face() -> str:
    data = base64.b64encode((FONTS / "poppins-latin-700-normal.woff2").read_bytes()).decode()
    body = base64.b64encode((FONTS / "roboto-latin-400-normal.woff2").read_bytes()).decode()
    return (f'@font-face {{ font-family: H; font-weight: 700; src: url(data:font/woff2;base64,{data}) format("woff2"); }}'
            f'@font-face {{ font-family: B; font-weight: 400; src: url(data:font/woff2;base64,{body}) format("woff2"); }}')


BACKDROP = "radial-gradient(circle at 30% 20%, #3a4149, #23282c 75%)"
ART = {
    # name: (width, height, transparent, page body)
    "logo-1024.png": (1024, 1024, True, f'<div style="width:1024px;height:1024px;padding:40px">{LOGO}</div>'),
    "logo-512.png": (512, 512, True, f'<div style="width:512px;height:512px;padding:20px">{LOGO}</div>'),
    # A Discord avatar is cropped to a circle, so the shield sits well inside the square.
    "discord-icon-1024.png": (1024, 1024, False, f'<div style="width:1024px;height:1024px;padding:150px;background:{BACKDROP}">{LOGO}</div>'),
    "header.png": (1280, 320, False, f'''<div style="width:1280px;height:320px;background:{BACKDROP};display:flex;align-items:center;gap:40px;padding:0 70px">
        <div style="width:210px;height:210px;flex:none">{LOGO}</div>
        <div><div style="font:700 84px/1.1 H;color:#f2f4f6">{NAME}</div><div style="font:400 28px/1.3 B;color:#b6bfc7;margin-top:10px">{TAGLINE}</div></div></div>'''),
    "social-preview.png": (1280, 640, False, f'''<div style="width:1280px;height:640px;background:{BACKDROP};display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px">
        <div style="width:300px;height:300px">{LOGO}</div>
        <div style="font:700 78px/1.1 H;color:#f2f4f6">{NAME}</div><div style="font:400 28px/1.3 B;color:#b6bfc7">{TAGLINE}</div></div>'''),
    "discord-cover.png": (1024, 576, False, f'''<div style="width:1024px;height:576px;background:{BACKDROP};display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px">
        <div style="width:270px;height:270px">{LOGO}</div>
        <div style="font:700 66px/1.1 H;color:#f2f4f6">{NAME}</div></div>'''),
    # Discord lays the bot's picture over the lower left of its banner, so the words sit to the right of it.
    "discord-banner.png": (680, 240, False, f'''<div style="width:680px;height:240px;background:{BACKDROP};display:flex;align-items:center;justify-content:flex-end;padding-right:36px;text-align:right">
        <div><div style="font:700 52px/1.1 H;color:#f2f4f6">{NAME}</div><div style="font:400 18px/1.35 B;color:#b6bfc7;margin-top:6px;max-width:400px">{TAGLINE}</div></div></div>'''),
}


def main() -> None:
    out = ROOT / "assets"
    out.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        for name, (width, height, transparent, body) in ART.items():
            page = browser.new_page(viewport={"width": width, "height": height})
            page.set_content(f"<!doctype html><meta charset='utf-8'><style>{font_face()} *{{box-sizing:border-box;margin:0}} html,body{{background:transparent;width:{width}px;height:{height}px;overflow:hidden}} svg{{display:block;width:100%;height:100%}}</style>{body}")
            page.wait_for_timeout(250)
            page.evaluate("document.fonts.ready")
            page.screenshot(path=str(out / name), omit_background=transparent)
            page.close()
            print(out / name)
        browser.close()


if __name__ == "__main__":
    main()
