"""The welcome picture's drawing code: text, fonts, backgrounds and the renderer. Nothing here needs Discord or a database."""
from __future__ import annotations

import io
import math
import struct
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

from PIL import Image, ImageChops, ImageDraw, ImageFont

from captainguild.core.stats import draw as cards
from captainguild.core.welcomecard import backgrounds, fonts, gif, model, render, text
from captainguild.core.welcomecard.render import Inputs, Raw

M = text.Marked.of
FONT_DIR = Path(fonts.FONT_DIR)


def png(width: int = 64, height: int = 64, color=(200, 40, 40), mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, (width, height), color).save(out, format="PNG")
    return out.getvalue()


def face_png(side: int = 128) -> bytes:
    """A stand-in for an avatar: a blue disc on yellow, so its parts can be told apart in a drawn picture."""
    image = Image.new("RGB", (side, side), (240, 180, 60))
    ImageDraw.Draw(image).ellipse((side // 6, side // 6, side - side // 6, side - side // 6), fill=(40, 90, 200))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def frame_color(index: int) -> tuple[int, int, int]:
    """The background of one frame of `moving_gif`. No two frames share one, so none is merged with its neighbor when the file is written."""
    return (index * 67) % 200 + 20, (index * 29) % 200 + 20, (index * 113) % 200 + 20


def moving_gif(width: int = 120, height: int = 60, frames: int = 6, duration: int | list[int] = 80, loop: int | None = 0) -> bytes:
    """An animation whose every frame is one flat color."""
    made = [Image.new("RGB", (width, height), frame_color(index)) for index in range(frames)]
    out = io.BytesIO()
    extra = {} if loop is None else {"loop": loop}
    made[0].save(out, format="GIF", save_all=True, append_images=made[1:], duration=duration, **extra)
    return out.getvalue()


def drifting_gif(width: int = 120, height: int = 60, frames: int = 5) -> bytes:
    """A yellow square crossing a background that never changes. Pillow writes each later frame as only the patch that changed, the way
    most GIFs are stored."""
    made = []
    for index in range(frames):
        image = Image.new("RGB", (width, height), (20, 60, 160))
        ImageDraw.Draw(image).rectangle((5 + index * 20, 20, 24 + index * 20, 39), fill=(255, 255, 0))
        made.append(image)
    out = io.BytesIO()
    made[0].save(out, format="GIF", save_all=True, append_images=made[1:], duration=80, loop=0)
    return out.getvalue()


GIF_COLORS = [(0, 0, 0), (200, 40, 40), (40, 160, 60), (40, 60, 200), (255, 255, 255), (250, 200, 30)]
RED, GREEN, BLUE, WHITE, YELLOW = GIF_COLORS[1:]


def flat_lzw(index: int, pixels: int) -> bytes:
    """GIF LZW data at code size 8 for `pixels` pixels of one palette index. A clear code before every 200 of them keeps every code 9 bits
    wide."""
    codes: list[int] = []
    for start in range(0, pixels, 200):
        codes += [256] + [index] * min(200, pixels - start)
    return lzw_codes(codes + [257])


def lzw_codes(codes: list[int], width: int = 9) -> bytes:
    """GIF LZW codes, each written `width` bits wide: 9 at code size 8. The caller keeps the table from growing with clear codes."""
    out, held, bits = bytearray(), 0, 0
    for code in codes:
        held |= code << bits
        bits += width
        while bits >= 8:
            out.append(held & 0xFF)
            held >>= 8
            bits -= 8
    if bits:
        out.append(held)
    return bytes(out)


def loop_block(count: int, name: bytes = b"NETSCAPE2.0") -> bytes:
    return b"!" + bytes([0xFF, 11]) + name + bytes([3, 1]) + struct.pack("<H", count) + bytes(1)


def hand_gif(screen: tuple[int, int], frames: list[dict], *, colors: list | None = GIF_COLORS, loop: int | None = 0, tail: bytes = b";") -> bytes:
    """A GIF written block by block, the way other programs write them. Each frame is a dict: box (left, top, width, height), index (its one
    palette color) or data (its LZW codes as lzw_codes writes them), disposal, clear (its transparent index), plain (no control block),
    local (colors of its own), code (its LZW code size, 8 unless given) and before (bytes written ahead of it, another block or a stray
    byte). The file names white as its background color, and with colors=None it has no colors of its own for the frames to share."""
    out = bytearray(b"GIF89a" + struct.pack("<HHBBB", screen[0], screen[1], 0x70 if colors is None else 0xF7, 4, 0))
    for index in range(256 if colors is not None else 0):
        out += bytes(colors[index] if index < len(colors) else (0, 0, 0))
    if loop is not None:
        out += loop_block(loop)
    for frame in frames:
        left, top, wide, tall = frame.get("box", (0, 0, *screen))
        out += frame.get("before", b"")
        if not frame.get("plain"):
            control = frame.get("disposal", 0) << 2 | ("clear" in frame)
            out += b"!" + bytes([0xF9, 4, control]) + struct.pack("<H", frame.get("delay", 10)) + bytes([frame.get("clear", 0), 0])
        local = frame.get("local")
        out += b"," + struct.pack("<HHHHB", left, top, wide, tall, 0 if local is None else 0x87)
        for index in range(256 if local is not None else 0):
            out += bytes(local[index] if index < len(local) else (0, 0, 0))
        out += bytes([frame.get("code", 8)])
        data = frame["data"] if "data" in frame else flat_lzw(frame["index"], wide * tall)
        for start in range(0, len(data), 255):
            out += bytes([len(data[start:start + 255])]) + data[start:start + 255]
        out += bytes(1)
    return bytes(out + tail)


def design(**parts) -> dict:
    return model.clean_design({"width": 400, "height": 200, "background": {"kind": "color", "color": "#102030"}, **parts}, fonts.KEYS)


def opened(picture: render.Picture) -> Image.Image:
    return Image.open(io.BytesIO(picture.data))


def near(first, second, slack: int = 6) -> bool:
    return all(abs(a - b) <= slack for a, b in zip(first[:3], second[:3]))


class MarksTest(unittest.TestCase):
    def spans(self, written: str, outside: bytes | None = None):
        marked = M(written) if outside is None else text.Marked(written, outside)
        return [(span.text, span.bold, span.italic, span.underline, span.emoji) for span in text.parse(marked)]

    def test_the_three_marks(self):
        self.assertEqual(self.spans("**b** *i* __u__"), [("b", True, False, False, ""), (" ", False, False, False, ""), ("i", False, True, False, ""), (" ", False, False, False, ""), ("u", False, False, True, "")])

    def test_marks_nest(self):
        self.assertEqual(self.spans("__**n**__"), [("n", True, False, True, "")])
        self.assertEqual(self.spans("***both***"), [("both", True, True, False, "")])

    def test_a_mark_with_no_partner_is_drawn_as_written(self):
        self.assertEqual(self.spans("**open"), [("**open", False, False, False, "")])
        self.assertEqual(self.spans("5 * 3 * 2"), [("5 * 3 * 2", False, False, False, "")])
        self.assertEqual(self.spans("snake_case_name"), [("snake_case_name", False, False, False, "")])

    def test_a_backslash_keeps_a_star(self):
        self.assertEqual(self.spans("\\*not italic\\*"), [("*not italic*", False, False, False, "")])

    def test_a_server_emoji_is_its_own_span(self):
        self.assertEqual(self.spans("hi <:wave:123456789012345678>!"), [("hi ", False, False, False, ""), ("", False, False, False, "123456789012345678"), ("!", False, False, False, "")])
        self.assertEqual(text.server_emoji(M("<a:spin:223456789012345678> and <:x:323456789012345678>")), ["223456789012345678", "323456789012345678"])

    def test_text_from_outside_the_template_never_makes_a_mark(self):
        written = "Hi **Sam**"
        self.assertEqual(self.spans(written, bytes([0, 0, 0, 1, 1, 1, 1, 1, 1, 1])), [("Hi **Sam**", False, False, False, "")])
        emoji = "<:evil:123456789012345678>"
        self.assertEqual(self.spans(emoji, bytes([1] * len(emoji))), [(emoji, False, False, False, "")])

    def test_the_servers_own_marks_still_work_around_outside_text(self):
        written = "**Sam**"
        self.assertEqual(self.spans(written, bytes([0, 0, 1, 1, 1, 0, 0])), [("Sam", True, False, False, "")])

    def test_with_marks_off_everything_is_text(self):
        self.assertEqual([span.text for span in text.parse(text.Marked("**a**", b"\0" * 5, False))], ["**a**"])

    def test_characters_that_turn_text_around_are_taken_out(self):
        self.assertEqual(self.spans("ab\U0000202ecd\U00002066e\x00f\tg"), [("abcdef g", False, False, False, "")])

    def test_reading_a_texts_server_emoji_is_quick_however_its_marks_fall(self):
        texts = [text.Marked.of("***" * 333 + "a") for _ in range(40)]
        started = time.perf_counter()
        for marked in texts:
            self.assertEqual(text.server_emoji(marked), [])
        took = time.perf_counter() - started
        self.assertLess(took, 0.01, f"forty texts of a thousand stars and no emoji took {took * 1000:.0f} ms")
        texts = [text.Marked.of("<" + "***" * 333 + "a") for _ in range(40)]
        started = time.perf_counter()
        for marked in texts:
            self.assertEqual(text.server_emoji(marked), [])
        took = time.perf_counter() - started
        self.assertLess(took, 0.2, f"forty texts of a thousand stars, each read through, took {took * 1000:.0f} ms, where pairing every mark takes over 400")
        self.assertEqual(text.server_emoji(M("**<:a:123456789012345678>** \\<:b:223456789012345678> <a:c:323456789012345678>")),
                         ["123456789012345678", "323456789012345678"], "the same emoji the full reading finds")

    def test_a_long_text_is_cut_to_what_can_be_drawn(self):
        self.assertEqual(len(text.parse(text.Marked("x" * 5000, b"", False))[0].text), text.MAX_DRAWN)


class FontsTest(unittest.TestCase):
    def test_every_family_has_its_files_and_its_license(self):
        for family in fonts.FAMILIES.values():
            for _, file in family.files:
                self.assertTrue((FONT_DIR / file).is_file(), file)
            stem = "DejaVu" if family.key == "dejavu" else family.files[0][1].rsplit(".", 1)[0]
            self.assertTrue((FONT_DIR / f"{stem}-LICENSE.txt").is_file(), family.key)

    def test_twelve_families_ship_beside_dejavu_within_four_megabytes(self):
        added = [family for family in fonts.FAMILIES.values() if family.key != "dejavu"]
        self.assertTrue(8 <= len(added) <= 12, len(added))
        self.assertLessEqual(sum(path.stat().st_size for path in FONT_DIR.glob("*.ttf")), 4 * 1000 * 1000)

    def test_every_family_loads_at_every_weight_it_names(self):
        for family in fonts.FAMILIES.values():
            for weight in family.weights:
                source = fonts.chain(family.key, weight)[0]
                self.assertGreater(source.at(40).getlength("Welcome"), 20, (family.key, weight))

    def test_a_weight_axis_changes_how_wide_the_letters_are(self):
        light, heavy = fonts.chain("manrope", 200)[0].at(60), fonts.chain("manrope", 800)[0].at(60)
        self.assertGreater(heavy.getlength("Welcome home"), light.getlength("Welcome home") + 5)

    def test_a_font_files_character_table_is_read(self):
        table = fonts.coverage(str(FONT_DIR / "DejaVuSans.ttf"))
        self.assertGreater(len(table), 4000)
        for letter in "A\U000000e9\U00000416\U000005d0\U00000639":
            self.assertIn(ord(letter), table, hex(ord(letter)))
        self.assertNotIn(0x4E2D, table)   # a Chinese character DejaVu doesn't have
        self.assertNotIn(0xE000, table)   # a private code point no font here has

    def test_the_search_falls_back_in_a_fixed_order(self):
        chain = fonts.chain("pacifico", 400)
        self.assertEqual(Path(chain[0].path).name, "Pacifico.ttf")
        self.assertEqual(Path(chain[1].path).name, "DejaVuSans.ttf")
        self.assertEqual([source.path for source in chain[2:]], list(fonts.host_fonts()))
        self.assertEqual(fonts.pick(chain, "W"), 0)
        self.assertEqual(fonts.pick(chain, "\U000005d0"), 1)  # Hebrew, which Pacifico lacks and DejaVu has
        self.assertEqual(fonts.chain("pacifico", 400), fonts.chain("pacifico", 400))

    def test_bold_takes_a_heavier_weight_where_the_family_has_one(self):
        self.assertEqual(fonts.chain("manrope", 400, True)[0].weight, 700)
        self.assertFalse(fonts.chain("manrope", 400, True)[0].thicken)
        self.assertTrue(fonts.chain("anton", 400, True)[0].thicken)  # one weight only, so its bold is drawn thicker
        self.assertEqual(Path(fonts.chain("manrope", 400, True)[1].path).name, "DejaVuSans-Bold.ttf")

    def test_a_styled_letter_no_font_has_reads_as_its_plain_form(self):
        self.assertEqual(fonts.simpler("\U0001d4d0"), "A")
        self.assertEqual(fonts.simpler("\U0000ff21"), "A")
        self.assertEqual(fonts.simpler("A"), "")

    def test_linux_fonts_are_ranked_the_same_way_every_time(self):
        names = ["NotoSansThai-Regular.ttf", "DroidSansFallbackFull.ttf", "NotoSansCJK-Regular.ttc", "NotoSans-Regular.ttf", "NotoSans-Italic.ttf", "NotoColorEmoji.ttf", "NotoSansMono-Regular.ttf", "Other.ttf"]
        ranked = sorted(name for name in names if fonts._host_rank(name) is not None)
        ranked.sort(key=lambda name: fonts._host_rank(name))
        self.assertEqual(ranked, ["NotoSans-Regular.ttf", "NotoSansCJK-Regular.ttc", "DroidSansFallbackFull.ttf", "NotoSansThai-Regular.ttf"])


class LayoutTest(unittest.TestCase):
    def found(self, written: str, width: int = 300, height: int = 120, **setting):
        return text.layout(M(written), text.Setting(**{"size": 40, **setting}), width, height)

    def test_text_that_fits_keeps_its_size(self):
        found = self.found("Hi")
        self.assertEqual((found.size, len(found.lines), found.shrunk, found.cut), (40, 1, False, False))

    def test_a_long_line_wraps_at_a_space(self):
        found = self.found("one two three four five six", width=220, height=400)
        self.assertGreater(len(found.lines), 1)
        self.assertEqual(found.size, 40)
        for line in found.lines:
            self.assertLessEqual(line.width, 220.5)
            self.assertNotEqual(line.placed[-1].piece.kind, "space")

    def test_with_wrap_off_only_written_line_breaks_count(self):
        found = self.found("one two three four five six\nseven", width=220, height=400, wrap=False, shrink=False)
        self.assertEqual(len(found.lines), 2)

    def test_text_shrinks_until_it_fits_its_box(self):
        found = self.found("Welcome to the server, it is good to see you here", width=260, height=90)
        self.assertLess(found.size, 40)
        self.assertTrue(found.shrunk)
        self.assertLessEqual(len(found.lines) * found.advance, 90.5)
        for line in found.lines:
            self.assertLessEqual(line.width, 260.5)
        bigger = self.found("Welcome to the server, it is good to see you here", width=520, height=180)
        self.assertGreater(bigger.size, found.size)

    def test_a_word_wider_than_the_box_shrinks_before_it_is_cut(self):
        found = self.found("supercalifragilistic", width=200, height=100)
        self.assertEqual(len(found.lines), 1)
        self.assertLess(found.size, 40)

    def test_what_cant_fit_at_the_smallest_size_ends_in_an_ellipsis(self):
        found = self.found("word " * 300, width=120, height=30)
        self.assertTrue(found.cut)
        self.assertEqual(found.size, text.SMALLEST)
        last = found.lines[-1].placed[-1].piece.text
        self.assertTrue(last.endswith(text.ELLIPSIS) or last.endswith("..."))
        self.assertLessEqual(len(found.lines) * found.advance, 30.5)

    def test_with_shrink_off_the_size_stays(self):
        found = self.found("word " * 50, width=120, height=30, shrink=False)
        self.assertEqual(found.size, 40)
        self.assertFalse(found.cut)

    def test_the_nine_positions(self):
        for align, valign in ((a, v) for a in ("left", "center", "right") for v in ("top", "middle", "bottom")):
            found = self.found("Hi", width=300, height=200, align=align, valign=valign)
            line = found.lines[0]
            left = line.placed[0].x
            self.assertAlmostEqual(left, {"left": 0, "center": (300 - line.width) / 2, "right": 300 - line.width}[align], delta=0.6, msg=(align, valign))
            self.assertAlmostEqual(found.top, {"top": 0, "middle": (200 - found.advance) / 2, "bottom": 200 - found.advance}[valign], delta=0.6, msg=(align, valign))

    def test_each_line_is_aligned_on_its_own(self):
        found = self.found("a long first line\nshort", width=400, height=200, align="right")
        ends = [line.placed[-1].x + line.placed[-1].width for line in found.lines]
        self.assertAlmostEqual(ends[0], 400, delta=1)
        self.assertAlmostEqual(ends[1], 400, delta=1)

    def test_letter_spacing_widens_a_line(self):
        plain = self.found("spacing", width=600, wrap=False, shrink=False).lines[0].width
        spaced = self.found("spacing", width=600, wrap=False, shrink=False, letter_spacing=10).lines[0].width
        self.assertAlmostEqual(spaced - plain, 60, delta=8)

    def test_line_height_sets_the_step_between_lines(self):
        self.assertAlmostEqual(self.found("a\nb", height=400, line_height=2.0).advance, 80)

    def test_names_in_other_scripts_get_a_font_that_has_them(self):
        chains = {False: fonts.chain("fredoka", 500), True: fonts.chain("fredoka", 500, True)}
        lines, _ = text.pieces_of(text.parse(M("Sam \U00000416\U00000435\U0000043d\U0000044f")), lambda bold: chains[bold])
        used = [(piece.text, piece.font) for piece in lines[0] if piece.kind == "text"]
        self.assertEqual(used, [("Sam", 0), ("\U00000416\U00000435\U0000043d\U0000044f", 1)])

    def drawn_by(self, written: str, family: str = "fredoka", outside: bytes | None = None) -> list[tuple[str, int]]:
        chains = {False: fonts.chain(family, 400), True: fonts.chain(family, 400, True)}
        marked = M(written) if outside is None else text.Marked(written, outside)
        lines, _ = text.pieces_of(text.parse(marked), lambda bold: chains[bold])
        for piece in lines[0]:
            if piece.kind == "text":
                for character in piece.text:
                    if not text._drawn_nothing(character):
                        self.assertTrue(chains[piece.bold][piece.font].covers(character), f"U+{ord(character):04X} is drawn with a font that lacks it, as an empty box")
        return [(piece.text, piece.font) for piece in lines[0] if piece.kind == "text"]

    def two_lines(self, name: str) -> str:
        return f"Welcome {name}\nto The Server!"

    def test_a_name_ending_in_a_joiner_before_a_line_break_draws_and_keeps_the_break(self):
        joiner = chr(0x200D)
        names = [joiner, "a" + joiner, joiner + joiner, "#" + joiner, "Sam" + joiner, "a" + joiner + chr(0xFEFF), "a" + joiner + chr(0x200F),
                 chr(0x1F468) + joiner]
        for name in names:
            written = self.two_lines(name)
            marked = text.Marked(written, bytes(8) + bytes([1]) * len(name) + bytes(len(written) - 8 - len(name)))
            for family in ("caveat", "fredoka", "dejavu"):
                made = design(width=1000, height=300, layers=[{"type": "text", "x": 20, "y": 20, "w": 900, "h": 260, "text": "{x}", "font": family, "wrap": False, "shrink": False}])
                picture = render.render(made, Inputs(texts={made["layers"][0]["id"]: marked}))
                self.assertEqual(picture.kind, "png", repr(name))
            found = text.layout(marked, text.Setting(size=48, wrap=False, shrink=False), 900, 260)
            self.assertEqual(len(found.lines), 2, f"the line break after {name!r} is kept")

    def test_a_line_break_is_a_cluster_of_its_own_even_after_a_joiner(self):
        joiner = chr(0x200D)
        self.assertEqual(text._clusters("a" + joiner + "\nb"), ["a" + joiner, "\n", "b"])
        self.assertEqual(text._clusters(chr(0x1F468) + joiner + "\n" + chr(0x301) + "x"), [chr(0x1F468) + joiner, "\n", chr(0x301), "x"], "a mark that starts a line joins no line break")

    def test_a_joiner_that_ends_a_line_or_the_text_is_dropped(self):
        joiner = chr(0x200D)
        written = "Sam" + joiner + joiner + "\nto us" + joiner
        tidied = text._tidy(text.Marked(written, bytes([1]) * 5 + bytes(len(written) - 5)))
        self.assertEqual(tidied.text, "Sam\nto us")
        self.assertEqual(tidied.outside, bytes([1]) * 3 + bytes(6), "the outside notes stay in step")
        self.assertEqual(text._tidy(text.Marked.of(chr(0x1F468) + joiner + chr(0x1F469))).text, chr(0x1F468) + joiner + chr(0x1F469), "a joiner between two emoji stays")

    def test_text_the_shaper_refuses_is_drawn_without_shaping(self):
        from PIL import ImageDraw as Draw, ImageFont as Font
        real_length, real_text = Font.FreeTypeFont.getlength, Draw.ImageDraw.text

        def length(face, letters, *args, **kwargs):
            if face.layout_engine == Font.Layout.RAQM:
                raise RuntimeError("raqm_layout() failed.")
            return real_length(face, letters, *args, **kwargs)

        def write(pen, xy, letters, *args, **kwargs):
            if kwargs.get("font") is not None and kwargs["font"].layout_engine == Font.Layout.RAQM:
                raise RuntimeError("raqm_layout() failed.")
            return real_text(pen, xy, letters, *args, **kwargs)
        for words in ("Hello there", "*Hello there*"):
            made = design(layers=[{"type": "text", "x": 10, "y": 10, "w": 380, "h": 180, "text": words, "letter_spacing": 2}])
            with mock.patch.object(Font.FreeTypeFont, "getlength", length), mock.patch.object(Draw.ImageDraw, "text", write):
                picture = render.render(made, Inputs())
            drawn = opened(picture).convert("RGB")
            self.assertGreater(sum(1 for pixel in drawn.getdata() if pixel[0] > 200), 300, f"{words}: the letters are drawn, one by one")
        source = fonts.chain(fonts.DEFAULT, 400)[0]
        with mock.patch.object(Font.FreeTypeFont, "getlength", length):
            self.assertEqual(text._advance(source, 40, "Hello", False), source.at(40, shaped=False).getlength("Hello"), "measured as it is drawn")

    def test_right_to_left_text_the_shaper_refuses_is_drawn_from_the_right(self):
        from PIL import ImageDraw as Draw, ImageFont as Font
        word = "".join(map(chr, (0x5E9, 0x5DC, 0x5D5, 0x5DD)))  # a Hebrew word, whose letters a shaper lays from the right
        real = Draw.ImageDraw.text

        def write(pen, xy, letters, *args, **kwargs):
            if kwargs.get("font") is not None and kwargs["font"].layout_engine == Font.Layout.RAQM:
                raise RuntimeError("raqm_layout() failed.")
            return real(pen, xy, letters, *args, **kwargs)
        for written in (word + " " + word, "*" + word + "*"):
            chosen = text.Setting(family="dejavu", size=60, wrap=False, shrink=False)
            found = text.layout(M(written), chosen, 400, 100)
            shaped, _, _ = text.paint(found, chosen, 400, 100, 20, lambda emoji: None)
            with mock.patch.object(Draw.ImageDraw, "text", write):
                unshaped, _, _ = text.paint(found, chosen, 400, 100, 20, lambda emoji: None)
            ink = sum(1 for value in shaped.getdata() if value > 64)
            differ = sum(1 for value in ImageChops.difference(shaped, unshaped).getdata() if value > 64)
            self.assertLess(differ, ink // 10, f"{written!a}: drawn without shaping, the letters stand where the shaper puts them")

    def test_a_runtime_error_from_the_drawing_libraries_is_refused_with_a_sentence(self):
        made = design(layers=[{"type": "text", "x": 10, "y": 10, "w": 380, "h": 180, "text": "Hi"}])
        with mock.patch.object(text, "paint", side_effect=RuntimeError("raqm_layout() failed.")), self.assertLogs("captainguild.core.welcomecard.render", "INFO"):
            with self.assertRaises(render.RenderError) as refused:
                render.render(made, Inputs())
        self.assertEqual(str(refused.exception), render.UNDRAWABLE)

    def test_a_mark_the_font_lacks_is_drawn_by_a_font_that_has_it(self):
        strike = chr(0x0336)
        self.assertFalse(fonts.chain("fredoka", 400)[0].covers(strike), "the family a server starts with has no long stroke")
        used = self.drawn_by("S" + strike + "a" + strike + "m" + strike + " Carter")
        self.assertEqual(used[0], ("S" + strike + "a" + strike + "m" + strike, 1), "the letters with their strokes go to the first font that has both")
        self.assertEqual(used[-1], ("Carter", 0), "letters with no marks keep the family chosen")
        for family in ("manrope", "orbitron", "pressstart", "lora"):
            self.drawn_by("Z" + chr(0x0337) * 3 + "a" + chr(0x0352) + chr(0x0353) + "l", family)

    def test_a_selector_or_a_joiner_never_sends_a_letter_to_another_font(self):
        self.assertFalse(fonts.chain("fredoka", 400)[0].covers(chr(0xFE0E)))
        name = "Rene" + chr(0x0301) + chr(0xFE0E) + " a" + chr(0x200D) + "b"
        outside = bytes([1]) * len(name)  # the whole name noted as outside
        self.assertEqual(self.drawn_by(name, outside=outside), [("Ren" + chr(0x00E9) + chr(0xFE0E), 0), ("a" + chr(0x200D) + "b", 0)])
        self.assertEqual(self.drawn_by(chr(0x200D) + "Sam", outside=bytes([1]) * 4), [(chr(0x200D) + "Sam", 0)], "nor does a joiner that starts a name")

    def test_a_name_of_joined_emoji_is_drawn_as_one_cluster(self):
        name = "\u200d".join(chr(0x1F600 + index) for index in range(16))
        asked = []
        real = cards.emoji_picture

        def watched(cluster):
            asked.append(cluster)
            return real(cluster)
        with mock.patch.object(cards, "emoji_picture", watched):
            chosen = text.Setting(size=40)
            found = text.layout(text.Marked(name, bytes([1]) * len(name)), chosen, 900, 100)
            text.paint(found, chosen, 900, 100, 20, lambda emoji: None)
        self.assertIn(name, asked, "the chain is asked for whole, as statistics cards always asked for it")

    def test_a_name_typed_in_decomposed_form_is_drawn_composed(self):
        name = "Vie" + chr(0x0323) + chr(0x0302) + "t"
        outside = bytes([1, 1, 1, 0, 0, 1])  # as a filled template notes it: a name's plain letters came from outside, and its marks can't be told apart
        self.assertEqual(text.parse(text.Marked(name, outside))[0].text, "Vi" + chr(0x1EC7) + "t")
        self.assertEqual(self.drawn_by(name, outside=outside), [("Vi", 0), (chr(0x1EC7), 1), ("t", 0)], "the composed letter is drawn whole by the first font that has it")

    def test_a_character_with_no_marks_is_kept_as_its_owner_chose_it(self):
        def kept(written: str) -> str:
            return text._tidy(text.Marked(written, bytes(len(written)))).text
        kanji = "".join(map(chr, (0xFA19, 0x8C37, 0x20, 0xFA1B, 0x7530, 0x20, 0xFA4F, 0x4ECB)))  # traditional forms Japanese fonts draw apart
        self.assertEqual(kept(kanji), kanji)
        others = "".join(map(chr, (0x212A, 0x212B, 0x2329, 0x78, 0x232A, 0x1F71, 0xFB2A, 0x0958, 0x2126)))  # Kelvin, Angstrom, angle brackets, oxia, ...
        self.assertEqual(kept(others), others)
        self.assertEqual(text._tidy(text.Marked("e" + chr(0x0301), bytes([1, 0]))).text, chr(0x00E9), "a name's letter with a mark is still composed")
        self.assertEqual(kept(chr(0x00EA) + chr(0x0323)), chr(0x1EC7))

    def test_old_korean_written_in_jamo_is_kept_as_written(self):
        for jamo in ("".join(map(chr, (0x1100, 0x1161, 0x11EB))), "".join(map(chr, (0x1100, 0x1161)))):  # jamo are letters, not marks
            self.assertEqual(text._tidy(text.Marked(jamo, bytes(len(jamo)))).text, jamo)

    def test_a_name_never_joins_what_the_server_wrote_beside_it(self):
        chains = {False: fonts.chain("dejavu", 400), True: fonts.chain("dejavu", 400, True)}

        def pieces(server: str, name: str, after: str = "", marks: bool = True) -> list[tuple[str, str]]:
            # Noted the way a template engine that marks every character of a name notes it.
            marked = text.Marked(server + name + after, bytes(len(server)) + bytes([1]) * len(name) + bytes(len(after)), marks)
            lines, _ = text.pieces_of(text.parse(marked), lambda bold: chains[bold])
            return [(piece.kind, piece.text) for piece in lines[0]]
        wave = chr(0x1F44B)
        for start in (chr(0x200D), chr(0xFE0F), chr(0x1F3FB)):  # a joiner, an emoji selector and a skin tone
            self.assertEqual(pieces(wave, start + "Sam")[0], ("emoji", wave), f"U+{ord(start):04X} joined the server's wave")
        self.assertEqual(pieces("#", chr(0x20E3) + "Sam")[0], ("text", "#"), "the server's # stays a #, not a keycap")
        self.assertEqual(pieces(chr(0x00FC), chr(0x0304) + "Sam")[:2], [("text", chr(0x00FC)), ("text", chr(0x0304) + "Sam")],
                         "the name's mark neither composes with the server's letter nor shares its piece")
        laptop = chr(0x1F4BB)
        self.assertEqual(pieces("", chr(0x1F468) + chr(0x200D), laptop)[-1], ("emoji", laptop), "a name that ends in a joiner leaves the server's emoji after it")
        self.assertEqual(pieces("Hi #", "Sam"), [("text", "Hi"), ("space", " "), ("text", "#Sam")], "where nothing joins, the name shares the server's piece")
        self.assertEqual(pieces("x*", chr(0x200D) + "Sam")[0], ("text", "x*"), "a star left as written keeps the notes in step")
        self.assertEqual(pieces("x" + chr(92) + "*", chr(0x200D) + "Sam")[0], ("text", "x*"), "and so does an escaped one")
        self.assertEqual(pieces(wave, chr(0x200D) + "Sam", marks=False)[0], ("emoji", wave), "with the marks off as well")
        unassigned = chr(0x0378)  # no font has it, so it is left out
        self.assertEqual(pieces("a" + chr(0x200D), unassigned + "Sam")[0], ("text", "a" + chr(0x200D)), "what follows a cluster left out stays apart")
        with mock.patch.object(fonts, "host_fonts", return_value=()):
            fonts.chain.cache_clear()
            try:
                bundled = {False: fonts.chain("fredoka", 500), True: fonts.chain("fredoka", 500, True)}
                styled = text.Marked("a" + chr(0x200D) + chr(0x1D4E2) + "am", bytes([0, 0, 1, 1, 1]))  # a script S no bundled font has
                lines, _ = text.pieces_of(text.parse(styled), lambda bold: bundled[bold])
            finally:
                fonts.chain.cache_clear()
        self.assertEqual([(piece.kind, piece.text) for piece in lines[0]], [("text", "a" + chr(0x200D)), ("text", "Sam")], "a letter's plain form stays apart in its place")
        written = "<:wave:123456789012345678>"
        self.assertEqual(pieces(written, chr(0x200D) + "Sam")[0], ("custom", "123456789012345678"), "a server emoji stays one")

    def test_a_mark_the_server_wrote_after_a_name_stays_on_its_last_letter(self):
        chains = {False: fonts.chain("dejavu", 400), True: fonts.chain("dejavu", 400, True)}

        def pieces(written: str, outside: bytes) -> list[tuple[str, str]]:
            lines, _ = text.pieces_of(text.parse(text.Marked(written, outside)), lambda bold: chains[bold])
            return [(piece.kind, piece.text) for piece in lines[0]]
        acute, circumflex = chr(0x0301), chr(0x0302)
        self.assertEqual(pieces("Saq" + acute, bytes([1, 1, 1, 0])), [("text", "Saq" + acute)])
        self.assertEqual(pieces("Saq" + acute + circumflex + "!", bytes([1, 1, 1, 0, 0, 0])), [("text", "Saq" + acute + circumflex + "!")])

        def named(written: str) -> bytes:  # a name's plain letters noted as outside, and nothing else of it
            return bytes(1 if " " <= character <= "~" else 0 for character in written)
        strike = chr(0x0336)
        struck = "S" + strike + "a" + strike + "m" + strike
        self.assertEqual(pieces(struck, named(struck)), [("text", struck)], "with only a name's plain letters noted, its own marks stay on them")
        keycap = "1" + chr(0xFE0F) + chr(0x20E3)
        self.assertEqual(pieces(keycap, named(keycap)), [("emoji", keycap)], "and its own keycap stays whole")
        joined = "a" + chr(0x200D) + "b"
        self.assertEqual(pieces(joined, named(joined)), [("text", joined)], "and so does its own joiner")

    def test_a_joiner_or_a_selector_with_nothing_to_join_draws_nothing(self):
        chains = {False: fonts.chain("dejavu", 400), True: fonts.chain("dejavu", 400, True)}

        def pieces(written: str, outside: bytes) -> list[tuple[str, str]]:
            lines, _ = text.pieces_of(text.parse(text.Marked(written, outside)), lambda bold: chains[bold])
            return [(piece.kind, piece.text) for piece in lines[0]]
        wave, joiner = chr(0x1F44B), chr(0x200D)
        self.assertEqual(pieces(wave + chr(0xFE0F) + "Sam", bytes([0, 1, 1, 1, 1])), [("emoji", wave), ("text", "Sam")], "no dotted circle where the selector was")
        self.assertEqual(pieces(chr(0xFE0E) + "Sam", bytes(4)), [("text", "Sam")])
        self.assertEqual(pieces("a" + joiner + chr(0xFE0F) + "Sam", bytes([0, 0, 1, 1, 1, 1])), [("text", "a" + joiner), ("text", "Sam")],
                         "what follows it still starts a piece of its own")
        self.assertEqual(pieces(joiner + "Sam", bytes(4)), [("text", joiner + "Sam")], "a joiner with a letter after it stays")

    def test_composing_keeps_the_note_of_what_came_from_outside(self):
        name = "**Re" + chr(0x0301) + "my**"
        written = "Hi **" + name + "**"
        outside = bytes([0] * 5 + [1] * len(name) + [0] * 2)
        spans = text.parse(text.Marked(written, outside))
        self.assertEqual([(span.text, span.bold) for span in spans], [("Hi ", False), ("**R" + chr(0x00E9) + "my**", True)],
                         "the server's stars still make the name bold, and the stars in the name stay stars")

    def test_a_styled_letter_falls_back_to_its_plain_form(self):
        with mock.patch.object(fonts, "host_fonts", return_value=()):
            fonts.chain.cache_clear()
            try:
                chains = {False: fonts.chain("fredoka", 500), True: fonts.chain("fredoka", 500, True)}
                lines, _ = text.pieces_of(text.parse(M("\U0001d4e2\U0001d4ea\U0001d4f6")), lambda bold: chains[bold])
            finally:
                fonts.chain.cache_clear()
        self.assertEqual("".join(piece.text for piece in lines[0]), "Sam")

    def test_a_character_nothing_can_draw_is_left_out(self):
        with mock.patch.object(fonts, "host_fonts", return_value=()):
            fonts.chain.cache_clear()
            try:
                chains = {False: fonts.chain("fredoka", 500), True: fonts.chain("fredoka", 500, True)}
                lines, _ = text.pieces_of(text.parse(M("a\U0000e123b")), lambda bold: chains[bold])
            finally:
                fonts.chain.cache_clear()
        self.assertEqual("".join(piece.text for piece in lines[0]), "ab")

    def test_right_to_left_words_keep_their_place_in_a_left_to_right_line(self):
        chains = {False: fonts.chain("dejavu", 400), True: fonts.chain("dejavu", 400, True)}
        lines, bases = text.pieces_of(text.parse(M("go \U000005e9\U000005dc\U000005d5\U000005dd \U000005e2\U000005d5\U000005dc\U000005dd now")), lambda bold: chains[bold])
        self.assertEqual(bases, [False])
        shown = [piece.text for piece in text._visual(lines[0], False) if piece.kind == "text"]
        self.assertEqual(shown, ["go", "\U000005e2\U000005d5\U000005dc\U000005dd", "\U000005e9\U000005dc\U000005d5\U000005dd", "now"])

    def test_a_line_that_starts_right_to_left_is_laid_out_from_the_right(self):
        chains = {False: fonts.chain("dejavu", 400), True: fonts.chain("dejavu", 400, True)}
        lines, bases = text.pieces_of(text.parse(M("\U000005e9\U000005dc\U000005d5\U000005dd Sam")), lambda bold: chains[bold])
        self.assertEqual(bases, [True])
        self.assertEqual([piece.text for piece in text._visual(lines[0], True) if piece.kind == "text"], ["Sam", "\U000005e9\U000005dc\U000005d5\U000005dd"])

    def test_nothing_a_person_can_type_makes_layout_raise(self):
        samples = ["", " ", "\n\n\n", "\U0000200d\U0000200d", "\U0000fe0f", "\U0001f468\U0000200d\U0001f469\U0000200d\U0001f467", "\ud83d", "a\U00000301\U00000301\U00000301\U00000301", "\U00000e2a\U00000e27\U00000e31\U00000e2a\U00000e14\U00000e35", "\U0000202e\U0000202d", "\x00\x01",
                   "\U0001f1fa\U0001f1f8\U0001f1fa", "#\U0000fe0f\U000020e3", "\U00000645\U00000631\U0000062d\U00000628\U00000627 hello \U000005e9\U000005dc\U000005d5\U000005dd", "\U000e0067", "****", "__", "*", "<:a:1>", "\\", "x" * 3000, "\U00003042" * 400]
        for sample in samples:
            for wrap, shrink in ((True, True), (False, False)):
                setting = text.Setting(size=50, wrap=wrap, shrink=shrink, outline=3, letter_spacing=2 if wrap else 0)
                found = text.layout(M(sample), setting, 180, 70)
                text.paint(found, setting, 180, 70, 20, lambda emoji: None)


class PaintTest(unittest.TestCase):
    def painted(self, written: str, **setting):
        chosen = text.Setting(**{"size": 40, "outline": 0, **setting})
        found = text.layout(M(written), chosen, 300, 100)
        return text.paint(found, chosen, 300, 100, 20, lambda emoji: None)

    def test_ink_lands_inside_the_box(self):
        ink, outline, color = self.painted("Hello")
        self.assertIsNone(outline)
        self.assertIsNone(color)
        left, top, right, bottom = ink.getbbox()
        self.assertGreaterEqual(left, 18)
        self.assertLess(right, 200)
        self.assertGreaterEqual(top, 10)
        self.assertLessEqual(bottom, 90)

    def test_the_outline_reaches_past_the_letters_by_its_width(self):
        ink, outline, _ = self.painted("Hello", outline=6)
        inner, outer = ink.getbbox(), outline.getbbox()
        self.assertTrue(4 <= inner[0] - outer[0] <= 8, (inner, outer))
        self.assertTrue(4 <= outer[3] - inner[3] <= 8, (inner, outer))

    def test_bold_puts_down_more_ink(self):
        def amount(image):
            return sum(image.histogram()[128:])
        self.assertGreater(amount(self.painted("**Hello**", family="anton")[0]), amount(self.painted("Hello", family="anton")[0]) * 1.05)
        self.assertGreater(amount(self.painted("**Hello**", family="manrope", weight=400)[0]), amount(self.painted("Hello", family="manrope", weight=400)[0]) * 1.05)

    def test_italic_leans_to_the_right(self):
        upright, leaning = self.painted("llll", family="dejavu")[0], self.painted("*llll*", family="dejavu")[0]
        box = upright.getbbox()
        top = (box[0], box[1], box[2] + 12, box[1] + 6)
        self.assertGreater(leaning.crop(top).getbbox()[2], upright.crop(top).getbbox()[2])

    def test_underline_draws_a_bar_under_the_letters(self):
        plain, under = self.painted("mmmm")[0], self.painted("__mmmm__")[0]
        self.assertGreater(under.getbbox()[3], plain.getbbox()[3])

    def test_a_server_emoji_is_drawn_from_its_picture(self):
        chosen = text.Setting(size=40)
        found = text.layout(M("a <:dot:123456789012345678> b"), chosen, 300, 100)
        asked = []

        def picture(emoji):
            asked.append(emoji)
            return Image.new("RGBA", (64, 64), (255, 0, 0, 255))
        _, _, color = text.paint(found, chosen, 300, 100, 20, picture)
        self.assertEqual(asked, ["123456789012345678"])
        self.assertIsNotNone(color)
        box = color.getbbox()
        self.assertTrue(38 <= box[2] - box[0] <= 48, box)
        self.assertEqual(color.getpixel(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)), (255, 0, 0, 255))

    @unittest.skipUnless(cards.emoji_picture("\U0001f600") is not None, "this machine has no color emoji font")
    def test_a_standard_emoji_is_drawn_in_color(self):
        _, _, color = self.painted("hi \U0001f600")
        self.assertIsNotNone(color)
        self.assertIsNotNone(color.getbbox())


class BackgroundsTest(unittest.TestCase):
    def test_the_catalog_holds_at_least_forty_in_categories(self):
        rows = backgrounds.names()
        self.assertGreaterEqual(len(rows), 40)
        self.assertEqual(len({row["key"] for row in rows}), len(rows))
        self.assertEqual({row["category"] for row in rows}, set(backgrounds.CATEGORIES))
        for category in backgrounds.CATEGORIES:
            self.assertGreaterEqual(sum(1 for row in rows if row["category"] == category), 8, category)
        self.assertEqual(len(rows), len(backgrounds.CATALOG))

    def test_every_background_draws_at_any_size_and_the_same_way_twice(self):
        for row in backgrounds.names():
            for size in ((300, 90), (90, 300), (100, 100)):
                first = backgrounds.draw(row["key"], *size)
                self.assertEqual((first.size, first.mode), (size, "RGB"), row["key"])
                self.assertEqual(first.tobytes(), backgrounds.draw(row["key"], *size).tobytes(), row["key"])
                self.assertGreater(len(first.getcolors(maxcolors=100000) or [0] * 100), 1, f"{row['key']} is one flat color")

    def test_no_two_backgrounds_are_the_same_picture(self):
        seen = {}
        for row in backgrounds.names():
            picture = backgrounds.draw(row["key"], 160, 60).tobytes()
            self.assertNotIn(picture, seen, f"{row['key']} repeats {seen.get(picture)}")
            seen[picture] = row["key"]

    def test_a_background_is_sharp_on_the_largest_canvas(self):
        large = backgrounds.draw("hills", 2000, 600)
        self.assertEqual(large.size, (2000, 600))
        # A stretched thumbnail would have no hard edge anywhere. The hills have one where two greens meet.
        column = [large.getpixel((1000, y)) for y in range(250, 600)]
        self.assertTrue(any(sum(abs(a - b) for a, b in zip(first, second)) > 40 for first, second in zip(column, column[1:])))

    def test_an_unknown_name_gives_nothing(self):
        self.assertIsNone(backgrounds.draw("nope", 100, 100))
        self.assertIsNone(backgrounds.thumbnail("nope"))

    def test_a_thumbnail_is_a_small_png(self):
        data = backgrounds.thumbnail("aurora")
        image = Image.open(io.BytesIO(data))
        self.assertEqual((image.format, image.size), ("PNG", backgrounds.THUMB))
        self.assertLess(len(data), 60_000)

    def test_a_gradient_runs_from_its_first_color_to_its_last(self):
        down = backgrounds.gradient(100, 200, ["#ff0000", "#0000ff"], 90)
        self.assertTrue(near(down.getpixel((50, 0)), (255, 0, 0)))
        self.assertTrue(near(down.getpixel((50, 199)), (0, 0, 255)))
        self.assertTrue(near(down.getpixel((50, 100)), (127, 0, 128), 8))
        across = backgrounds.gradient(200, 100, ["#ff0000", "#00ff00", "#0000ff"], 0)
        self.assertTrue(near(across.getpixel((0, 50)), (255, 0, 0)))
        self.assertTrue(near(across.getpixel((100, 50)), (0, 255, 0), 8))
        self.assertTrue(near(across.getpixel((199, 50)), (0, 0, 255)))

    def test_a_gradient_can_fade_to_clear(self):
        fade = backgrounds.gradient(100, 100, ["#00000000", "#000000ff"], 90)
        self.assertLess(fade.getpixel((50, 2))[3], 12)
        self.assertGreater(fade.getpixel((50, 97))[3], 243)

    def test_stripes_run_the_way_they_are_named(self):
        colors = ["#ff0000", "#00ff00", "#0000ff"]
        flag = backgrounds.stripes(90, 90, colors, "horizontal")
        self.assertEqual([flag.getpixel((45, y))[:3] for y in (5, 45, 85)], [(255, 0, 0), (0, 255, 0), (0, 0, 255)])
        self.assertEqual(flag.getpixel((5, 5)), flag.getpixel((85, 5)))
        posts = backgrounds.stripes(90, 90, colors, "vertical")
        self.assertEqual([posts.getpixel((x, 45))[:3] for x in (5, 45, 85)], [(255, 0, 0), (0, 255, 0), (0, 0, 255)])

    def test_one_stripe_is_a_plain_color(self):
        self.assertEqual(backgrounds.stripes(20, 20, ["#123456"]).getcolors(), [(400, (0x12, 0x34, 0x56, 255))])

    def test_the_ready_made_stripe_sets_are_all_colors(self):
        self.assertGreaterEqual(len(backgrounds.STRIPE_SETS), 8)
        for name, colors in backgrounds.STRIPE_SETS:
            self.assertEqual([model.color(color) for color in colors], list(colors), name)


class ShapesTest(unittest.TestCase):
    def test_a_circle_is_round_and_even_on_every_side(self):
        mask = render.shape_mask("ellipse", 100, 100)
        self.assertEqual(mask.getpixel((50, 50)), 255)
        self.assertEqual(mask.getpixel((1, 1)), 0)
        self.assertEqual(mask.tobytes(), mask.transpose(Image.Transpose.FLIP_LEFT_RIGHT).tobytes())
        self.assertEqual(mask.tobytes(), mask.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes())

    def test_a_rounded_box_loses_the_same_from_every_corner(self):
        mask = render.shape_mask("rect", 120, 80, [16, 16, 16, 16])
        self.assertEqual(mask.tobytes(), mask.transpose(Image.Transpose.FLIP_LEFT_RIGHT).tobytes())
        self.assertEqual(mask.tobytes(), mask.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes())
        self.assertEqual(mask.getpixel((0, 0)), 0)
        self.assertEqual(mask.getpixel((0, 40)), 255)  # the left edge is whole, not shaved
        self.assertEqual(mask.getpixel((60, 0)), 255)

    def test_each_corner_takes_its_own_radius(self):
        mask = render.shape_mask("rect", 100, 100, [40, 0, 0, 0])
        self.assertEqual((mask.getpixel((2, 2)), mask.getpixel((97, 2)), mask.getpixel((97, 97)), mask.getpixel((2, 97))), (0, 255, 255, 255))
        mask = render.shape_mask("rect", 100, 100, [0, 0, 40, 0])
        self.assertEqual((mask.getpixel((2, 2)), mask.getpixel((97, 2)), mask.getpixel((97, 97)), mask.getpixel((2, 97))), (255, 255, 0, 255))

    def test_radii_too_large_for_the_box_are_scaled_to_fit(self):
        pill = render.shape_mask("rect", 200, 60, [500, 500, 500, 500])
        self.assertEqual(pill.getpixel((100, 30)), 255)
        self.assertEqual(pill.getpixel((2, 2)), 0)
        self.assertEqual(pill.getpixel((30, 30)), 255)

    def test_a_hexagon_has_six_sides(self):
        mask = render.shape_mask("hexagon", 200, 200)
        self.assertEqual(mask.getpixel((100, 100)), 255)
        self.assertEqual(mask.getpixel((100, 3)), 255)    # its top point
        self.assertEqual(mask.getpixel((3, 100)), 255)    # its flat left side
        self.assertEqual(mask.getpixel((3, 3)), 0)
        self.assertEqual(mask.getpixel((3, 30)), 0)       # above the left side, where a circle would also be empty
        self.assertEqual(mask.getpixel((4, 60)), 255)     # inside the hexagon, where a circle of the same size is empty
        self.assertEqual(render.shape_mask("ellipse", 200, 200).getpixel((4, 60)), 0)

    def test_an_inset_shape_is_smaller_by_that_much_all_round(self):
        inner = render.shape_mask("rect", 100, 100, [0, 0, 0, 0], 10)
        self.assertEqual(inner.getbbox(), (10, 10, 90, 90))

    def test_a_hexagons_ring_is_as_thick_across_a_slanted_side_as_across_a_flat_one(self):
        for wide, tall in ((300, 300), (300, 260), (260, 300)):
            for ring in (16, 32, 64):
                outer, inner = render.shape_mask("hexagon", wide, tall), render.shape_mask("hexagon", wide, tall, inset=ring)
                band = ImageChops.subtract(outer, inner)

                def across(dx: float, dy: float) -> float:
                    """How much of the ring a line from the middle crosses, at a tenth of a pixel."""
                    found = 0
                    for step in range(10 * max(wide, tall)):
                        x, y = wide / 2 + dx * step / 10, tall / 2 + dy * step / 10
                        if not (0 <= x < wide and 0 <= y < tall):
                            break
                        found += band.getpixel((int(x), int(y))) / 255
                    return found / 10
                # The upper right side runs from the top point to the right side's upper end. A line across it at a right angle measures it.
                side_x, side_y = wide / 2, tall / 4
                length = (side_x * side_x + side_y * side_y) ** 0.5
                flat, slanted = across(1, 0), across(side_y / length, -side_x / length)
                self.assertAlmostEqual(flat, ring, delta=1.2, msg=(wide, tall, ring))
                self.assertAlmostEqual(slanted, ring, delta=1.2, msg=f"{wide}x{tall}, ring {ring}: {slanted:.1f} across a slanted side, {flat:.1f} across a flat one")


class RenderTest(unittest.TestCase):
    def setUp(self):
        render.CACHE.clear()

    def test_a_design_becomes_a_png_of_the_canvas_size(self):
        picture = render.render(design(), Inputs())
        image = opened(picture)
        self.assertEqual((picture.kind, picture.filename, image.format, image.size, picture.frames), ("png", "welcome.png", "PNG", (400, 200), 1))
        self.assertEqual(image.convert("RGB").getpixel((200, 100)), (0x10, 0x20, 0x30))
        self.assertEqual(picture.notes, [])

    def test_the_same_design_and_inputs_give_the_same_bytes(self):
        made = design(layers=[{"type": "avatar", "x": 20, "y": 20, "w": 120, "h": 120, "stroke": {"color": "#ffffff", "width": 6}},
                              {"type": "text", "x": 160, "y": 20, "w": 220, "h": 160, "text": "Welcome **Sam**\nto the server", "stroke": {"color": "#000000", "width": 3}}],
                      background={"kind": "stock", "name": "dusk"})
        inputs = lambda: Inputs(avatar=Raw(face_png()), person=7 << 22)  # noqa: E731
        first = render.render(made, inputs())
        self.assertEqual(first.data, render.render(made, inputs()).data)
        render.CACHE.clear()
        self.assertEqual(first.data, render.render(made, inputs()).data, "a cold cache and a warm one must agree")

    def test_layers_are_drawn_from_the_bottom_up(self):
        made = design(layers=[{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 100, "fill": {"kind": "color", "color": "#ff0000"}},
                              {"type": "rect", "x": 50, "y": 50, "w": 100, "h": 100, "fill": {"kind": "color", "color": "#00ff00"}}])
        image = opened(render.render(made, Inputs())).convert("RGB")
        self.assertEqual((image.getpixel((25, 25)), image.getpixel((75, 75)), image.getpixel((125, 125)), image.getpixel((300, 25))), ((255, 0, 0), (0, 255, 0), (0, 255, 0), (0x10, 0x20, 0x30)))

    def test_a_hidden_layer_and_a_clear_one_are_not_drawn(self):
        made = design(layers=[{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 100, "hidden": True}, {"type": "rect", "x": 200, "y": 0, "w": 100, "h": 100, "opacity": 0}])
        self.assertEqual(opened(render.render(made, Inputs())).convert("RGB").getcolors(), [(80000, (0x10, 0x20, 0x30))])

    def test_opacity_lets_the_background_through(self):
        made = design(background={"kind": "color", "color": "#000000"}, layers=[{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 100, "fill": {"kind": "color", "color": "#ffffff"}, "opacity": 50}])
        self.assertTrue(near(opened(render.render(made, Inputs())).convert("RGB").getpixel((50, 50)), (127, 127, 127), 3))

    def test_a_see_through_color_is_see_through(self):
        made = design(background={"kind": "color", "color": "#000000"}, layers=[{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 100, "fill": {"kind": "color", "color": "#ffffff80"}}])
        self.assertTrue(near(opened(render.render(made, Inputs())).convert("RGB").getpixel((50, 50)), (128, 128, 128), 3))

    def test_no_background_gives_a_see_through_picture(self):
        made = design(background={"kind": "none"}, layers=[{"type": "ellipse", "x": 100, "y": 50, "w": 100, "h": 100, "fill": {"kind": "color", "color": "#ff0000"}}])
        image = opened(render.render(made, Inputs()))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((5, 5))[3], 0)
        self.assertEqual(image.getpixel((150, 100)), (255, 0, 0, 255))

    def test_a_stroke_sits_inside_the_box(self):
        made = design(layers=[{"type": "rect", "x": 100, "y": 50, "w": 100, "h": 100, "fill": {"kind": "color", "color": "#ff0000"}, "stroke": {"color": "#00ff00", "width": 10}}])
        image = opened(render.render(made, Inputs())).convert("RGB")
        self.assertEqual(image.getpixel((104, 100)), (0, 255, 0))
        self.assertEqual(image.getpixel((195, 100)), (0, 255, 0))
        self.assertEqual(image.getpixel((150, 54)), (0, 255, 0))
        self.assertEqual(image.getpixel((150, 100)), (255, 0, 0))
        self.assertEqual(image.getpixel((98, 100)), (0x10, 0x20, 0x30))

    def test_a_layer_turns_about_its_center(self):
        box = {"type": "rect", "x": 150, "y": 90, "w": 100, "h": 20, "fill": {"kind": "color", "color": "#ffffff"}}
        flat = opened(render.render(design(layers=[box]), Inputs())).convert("RGB")
        turned = opened(render.render(design(layers=[{**box, "rotation": 90}]), Inputs())).convert("RGB")
        self.assertEqual((flat.getpixel((160, 100)), flat.getpixel((200, 60))), ((255, 255, 255), (0x10, 0x20, 0x30)))
        self.assertEqual((turned.getpixel((160, 100)), turned.getpixel((200, 60))), ((0x10, 0x20, 0x30), (255, 255, 255)))
        slanted = opened(render.render(design(layers=[{**box, "rotation": 45}]), Inputs())).convert("RGB")
        self.assertEqual(slanted.getpixel((200, 100)), (255, 255, 255))
        self.assertTrue(near(slanted.getpixel((225, 125)), (255, 255, 255)))
        self.assertEqual(slanted.getpixel((225, 75)), (0x10, 0x20, 0x30))  # clockwise, so the right end goes down, not up

    def test_a_flip_mirrors_a_layer(self):
        fill = {"kind": "gradient", "angle": 0, "colors": ["#ff0000", "#0000ff"]}
        box = {"type": "rect", "x": 100, "y": 50, "w": 200, "h": 100, "fill": fill}
        plain = opened(render.render(design(layers=[box]), Inputs())).convert("RGB")
        flipped = opened(render.render(design(layers=[{**box, "flip_x": True}]), Inputs())).convert("RGB")
        self.assertTrue(near(plain.getpixel((105, 100)), (255, 0, 0), 12))
        self.assertTrue(near(flipped.getpixel((105, 100)), (0, 0, 255), 12))
        down = {"type": "rect", "x": 100, "y": 50, "w": 200, "h": 100, "fill": {"kind": "gradient", "angle": 90, "colors": ["#ff0000", "#0000ff"]}}
        self.assertTrue(near(opened(render.render(design(layers=[down]), Inputs())).convert("RGB").getpixel((200, 53)), (255, 0, 0), 20))
        self.assertTrue(near(opened(render.render(design(layers=[{**down, "flip_y": True}]), Inputs())).convert("RGB").getpixel((200, 53)), (0, 0, 255), 20))

    def test_a_layer_can_hang_off_every_edge(self):
        made = design(layers=[{"type": "ellipse", "x": -60, "y": -60, "w": 120, "h": 120}, {"type": "rect", "x": 350, "y": 150, "w": 120, "h": 120, "rotation": 30},
                              {"type": "text", "x": -40, "y": 170, "w": 300, "h": 80, "text": "edge", "size": 60}, {"type": "rect", "x": 900, "y": 900, "w": 50, "h": 50}])
        self.assertEqual(opened(render.render(made, Inputs())).size, (400, 200))

    # ---- the frame ----

    def test_the_frame_grows_the_picture_outward_on_every_side(self):
        made = design(frame={"color": "#ff00ff", "width": 16}, layers=[{"type": "rect", "x": 0, "y": 0, "w": 10, "h": 10, "fill": {"kind": "color", "color": "#ffffff"}}])
        image = opened(render.render(made, Inputs())).convert("RGB")
        self.assertEqual(image.size, (432, 232))
        frame = (255, 0, 255)
        for point in ((0, 0), (431, 0), (0, 231), (431, 231), (216, 8), (216, 224), (8, 116), (424, 116), (424, 20), (424, 210), (8, 20), (8, 210)):
            self.assertEqual(image.getpixel(point), frame, point)
        self.assertEqual(image.getpixel((16, 16)), (255, 255, 255), "the canvas starts right inside the frame")
        self.assertEqual(image.getpixel((415, 215)), (0x10, 0x20, 0x30))

    def test_the_background_is_fitted_to_the_canvas_and_not_to_the_frame(self):
        plain = opened(render.render(design(background={"kind": "stock", "name": "hills"}), Inputs())).convert("RGB")
        framed = opened(render.render(design(background={"kind": "stock", "name": "hills"}, frame={"color": "#000000", "width": 20}), Inputs())).convert("RGB")
        self.assertEqual(framed.crop((20, 20, 420, 220)).tobytes(), plain.tobytes())

    # ---- the avatar ----

    def avatar_picture(self, **layer):
        made = design(layers=[{"type": "avatar", "x": 100, "y": 0, "w": 200, "h": 200, **layer}])
        return opened(render.render(made, Inputs(avatar=Raw(face_png()), person=1 << 22))).convert("RGB")

    def test_the_four_avatar_shapes(self):
        backdrop, yellow = (0x10, 0x20, 0x30), (240, 180, 60)
        corner, hexagon_only = (104, 4), (104, 60)
        circle, rounded, square, hexagon = (self.avatar_picture(shape=shape) for shape in ("circle", "rounded", "square", "hexagon"))
        self.assertEqual(circle.getpixel(corner), backdrop)
        self.assertEqual(circle.getpixel(hexagon_only), backdrop)
        self.assertEqual(rounded.getpixel(corner), backdrop)
        self.assertTrue(near(rounded.getpixel((104, 100)), yellow))
        self.assertTrue(near(square.getpixel(corner), yellow))
        self.assertEqual(hexagon.getpixel(corner), backdrop)
        self.assertTrue(near(hexagon.getpixel(hexagon_only), yellow), "the hexagon is drawn, where a circle would be empty")
        for image in (circle, rounded, square, hexagon):
            self.assertTrue(near(image.getpixel((200, 100)), (40, 90, 200)), "the avatar's own picture is in the middle")

    def test_a_rounded_avatar_is_even_on_all_four_sides(self):
        made = design(layers=[{"type": "avatar", "x": 100, "y": 0, "w": 200, "h": 200, "shape": "rounded", "stroke": {"color": "#00ff00", "width": 8}}])
        image = opened(render.render(made, Inputs(avatar=Raw(png(color=(200, 40, 40)))))).convert("RGB")
        self.assertEqual(image.getpixel((104, 100)), (0, 255, 0))
        self.assertEqual(image.getpixel((200, 4)), (0, 255, 0), "the top edge has its ring, as the left one does")
        tile = image.crop((100, 0, 300, 200))
        self.assertEqual(tile.tobytes(), tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT).tobytes())
        self.assertEqual(tile.tobytes(), tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes())

    def test_the_ring_has_the_width_and_color_it_was_given(self):
        image = self.avatar_picture(shape="square", stroke={"color": "#00ff00", "width": 12})
        row = [image.getpixel((100 + x, 100)) for x in range(30)]
        self.assertEqual(sum(1 for pixel in row if pixel == (0, 255, 0)), 12)
        self.assertEqual(row[0], (0, 255, 0))
        none = self.avatar_picture(shape="square", stroke={"color": "#00ff00", "width": 0})
        self.assertTrue(near(none.getpixel((101, 100)), (240, 180, 60)))

    def test_an_avatar_that_cant_be_read_gets_a_stand_in_and_a_note(self):
        made = design(layers=[{"type": "avatar", "x": 100, "y": 0, "w": 200, "h": 200, "shape": "square", "stroke": {"color": "#ffffff", "width": 0}}])
        picture = render.render(made, Inputs(avatar=Raw(b"not a picture"), person=1 << 22))
        self.assertEqual(picture.notes, ["The avatar couldn't be read, so a stand-in is drawn."])
        self.assertTrue(near(opened(picture).convert("RGB").getpixel((105, 5)), render.STAND_IN_COLORS[1]))
        missing = render.render(made, Inputs(person=2 << 22))
        self.assertEqual(missing.notes, [])
        self.assertTrue(near(opened(missing).convert("RGB").getpixel((105, 5)), render.STAND_IN_COLORS[2]))

    def test_a_see_through_avatar_sits_on_the_layers_fill(self):
        clear = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        out = io.BytesIO()
        clear.save(out, format="PNG")
        made = design(layers=[{"type": "avatar", "x": 100, "y": 0, "w": 200, "h": 200, "shape": "square", "fill": {"kind": "color", "color": "#aa5500"}, "stroke": {"color": "#ffffff", "width": 0}}])
        self.assertEqual(opened(render.render(made, Inputs(avatar=Raw(out.getvalue())))).convert("RGB").getpixel((200, 100)), (0xAA, 0x55, 0x00))

    def test_the_avatars_color_is_its_vivid_color_and_not_a_muddy_average(self):
        logo = Image.new("RGB", (100, 100), (255, 255, 255))
        ImageDraw.Draw(logo).ellipse((25, 25, 75, 75), fill=(220, 30, 30))
        red, green, blue = render.dominant_color(logo)
        self.assertGreater(red, 180)
        self.assertLess(max(green, blue), 90)
        halves = Image.new("RGB", (100, 100), (30, 30, 200))
        ImageDraw.Draw(halves).rectangle((0, 0, 100, 45), fill=(40, 200, 40))
        picked = render.dominant_color(halves)
        self.assertTrue(near(picked, (30, 30, 200), 30), picked)  # the larger of two vivid colors, never a blend of them
        self.assertEqual(render.dominant_color(Image.new("RGBA", (10, 10), (0, 0, 0, 0))), render.BLANK_AVATAR)

    def test_an_avatar_color_fill_follows_the_avatar(self):
        made = design(background={"kind": "avatar"})
        blue = opened(render.render(made, Inputs(avatar=Raw(png(color=(20, 40, 220)))))).convert("RGB").getpixel((5, 5))
        green = opened(render.render(made, Inputs(avatar=Raw(png(color=(20, 200, 40)))))).convert("RGB").getpixel((5, 5))
        self.assertGreater(blue[2], blue[1] + 60)
        self.assertGreater(green[1], green[2] + 60)

    # ---- pictures ----

    def test_a_picture_layer_covers_its_box_from_the_center(self):
        wide = Image.new("RGB", (300, 100), (255, 0, 0))
        ImageDraw.Draw(wide).rectangle((100, 0, 199, 100), fill=(0, 0, 255))
        out = io.BytesIO()
        wide.save(out, format="PNG")
        made = design(layers=[{"type": "picture", "x": 150, "y": 50, "w": 100, "h": 100, "source": {"kind": "upload", "id": "wide"}}])
        image = opened(render.render(made, Inputs(pictures={"upload:wide": Raw(out.getvalue(), "wide")}))).convert("RGB")
        self.assertEqual({image.getpixel((155, 100)), image.getpixel((200, 100)), image.getpixel((245, 100))}, {(0, 0, 255)}, "only the middle third shows")

    def test_contain_and_stretch_fit_the_whole_picture(self):
        wide = Image.new("RGB", (300, 100), (255, 0, 0))
        out = io.BytesIO()
        wide.save(out, format="PNG")
        layer = {"type": "picture", "x": 150, "y": 50, "w": 100, "h": 100, "source": {"kind": "upload", "id": "wide"}}
        inputs = lambda: Inputs(pictures={"upload:wide": Raw(out.getvalue(), "wide")})  # noqa: E731
        contained = opened(render.render(design(layers=[{**layer, "fit": "contain"}]), inputs())).convert("RGB")
        self.assertEqual((contained.getpixel((200, 100)), contained.getpixel((200, 55))), ((255, 0, 0), (0x10, 0x20, 0x30)))
        stretched = opened(render.render(design(layers=[{**layer, "fit": "stretch"}]), inputs())).convert("RGB")
        self.assertEqual((stretched.getpixel((200, 100)), stretched.getpixel((200, 55))), ((255, 0, 0), (255, 0, 0)))

    def test_a_picture_from_an_address_is_drawn_and_a_missing_one_is_noted(self):
        layer = {"type": "picture", "id": "icon", "name": "Icon", "x": 0, "y": 0, "w": 100, "h": 100, "source": {"kind": "address", "address": "{server(icon)}"}, "fill": {"kind": "color", "color": "#333333"}}
        there = render.render(design(layers=[layer]), Inputs(pictures={"address:icon": Raw(png(color=(1, 200, 3)))}))
        self.assertEqual(opened(there).convert("RGB").getpixel((50, 50)), (1, 200, 3))
        self.assertEqual(there.notes, [])
        gone = render.render(design(layers=[layer]), Inputs())
        self.assertEqual(opened(gone).convert("RGB").getpixel((50, 50)), (0x33, 0x33, 0x33), "the fill shows where the picture would be")
        self.assertIn("The layer Icon has no picture to show", gone.notes[0])

    def test_an_upload_that_is_gone_is_noted_and_never_fatal(self):
        made = design(background={"kind": "upload", "id": "gone"}, layers=[{"type": "picture", "x": 0, "y": 0, "w": 50, "h": 50, "source": {"kind": "upload", "id": "gone"}}])
        picture = render.render(made, Inputs())
        self.assertEqual(opened(picture).size, (400, 200))
        self.assertIn("An uploaded picture this design names is gone, so the avatar's color stands in.", picture.notes)

    def test_a_file_that_is_not_an_image_is_never_decoded(self):
        made = design(background={"kind": "upload", "id": "bad"})
        for body in (b"GIF89a" + b"\x00" * 40, b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"RIFF\x00\x00\x00\x00WEBPVP8 ", b""):
            picture = render.render(made, Inputs(pictures={"upload:bad": Raw(body, "")}))
            self.assertEqual(picture.kind, "png")
            self.assertTrue(picture.notes)

    def test_a_stock_background_fills_the_canvas_and_an_unknown_one_is_noted(self):
        picture = render.render(design(background={"kind": "stock", "name": "hills"}), Inputs())
        self.assertEqual(opened(picture).convert("RGB").tobytes(), backgrounds.draw("hills", 400, 200).tobytes())
        self.assertEqual(render.render(design(background={"kind": "stock", "name": "retired"}), Inputs()).notes, ["A stock background this design names is gone, so the avatar's color stands in."])

    def test_stripes_and_gradients_fill_a_layer(self):
        made = design(layers=[{"type": "rect", "x": 0, "y": 0, "w": 90, "h": 90, "fill": {"kind": "stripes", "direction": "vertical", "colors": ["#ff0000", "#00ff00", "#0000ff"]}}])
        image = opened(render.render(made, Inputs())).convert("RGB")
        self.assertEqual([image.getpixel((x, 45)) for x in (5, 45, 85)], [(255, 0, 0), (0, 255, 0), (0, 0, 255)])

    # ---- text ----

    def text_picture(self, **layer):
        made = design(layers=[{"type": "text", "id": "t", "x": 50, "y": 50, "w": 300, "h": 100, "text": "Hello", "size": 60, "fill": {"kind": "color", "color": "#ffffff"}, **layer}])
        return render.render(made, Inputs(texts={"t": M(layer.get("text", "Hello"))}))

    def test_text_is_drawn_in_its_color_inside_its_box(self):
        picture = self.text_picture()
        image = opened(picture).convert("RGB")
        white = [(x, y) for x in range(0, 400, 2) for y in range(0, 200, 2) if image.getpixel((x, y)) == (255, 255, 255)]
        self.assertGreater(len(white), 100)
        self.assertTrue(all(48 <= x <= 352 and 48 <= y <= 152 for x, y in white))
        self.assertEqual(picture.sizes, {"t": 60})

    def test_an_outline_is_drawn_around_the_letters_in_its_color(self):
        image = opened(self.text_picture(stroke={"color": "#ff0000", "width": 5})).convert("RGB")
        colors = dict((color, count) for count, color in image.getcolors(maxcolors=100000))
        self.assertGreater(colors.get((255, 0, 0), 0), 300)
        self.assertGreater(colors.get((255, 255, 255), 0), 300)

    def test_the_size_a_text_was_shrunk_to_is_reported(self):
        picture = self.text_picture(text="a much longer line of text that has to shrink to fit")
        self.assertLess(picture.sizes["t"], 60)

    def test_text_too_long_for_its_box_is_noted(self):
        picture = self.text_picture(text="word " * 150, name="Greeting", w=80, h=20)
        self.assertEqual(picture.notes, ["The layer Greeting holds more text than its box has room for, so the end is cut."])

    def test_a_text_layer_with_no_filled_text_draws_what_was_written(self):
        made = design(layers=[{"type": "text", "id": "t", "x": 50, "y": 50, "w": 300, "h": 100, "text": "As written", "size": 40}])
        self.assertGreater(len(opened(render.render(made, Inputs())).convert("RGB").getcolors(maxcolors=100000)), 2)

    def test_text_can_take_a_gradient(self):
        image = opened(self.text_picture(text="MMMMMM", size=90, fill={"kind": "gradient", "angle": 0, "colors": ["#ff0000", "#0000ff"]})).convert("RGB")
        reds = sum(1 for x in range(50, 150, 2) for y in range(60, 140, 2) if image.getpixel((x, y))[0] > 180 and image.getpixel((x, y))[2] < 90)
        blues = sum(1 for x in range(250, 350, 2) for y in range(60, 140, 2) if image.getpixel((x, y))[2] > 180 and image.getpixel((x, y))[0] < 90)
        self.assertGreater(reds, 20)
        self.assertGreater(blues, 20)

    def test_a_server_emoji_in_text_comes_from_its_picture(self):
        made = design(layers=[{"type": "text", "id": "t", "x": 50, "y": 50, "w": 300, "h": 100, "text": "<:dot:123456789012345678>", "size": 60}])
        picture = render.render(made, Inputs(texts={"t": M("<:dot:123456789012345678>")}, pictures={"emoji:123456789012345678": Raw(png(color=(0, 255, 0)))}))
        self.assertGreater(dict((color, count) for count, color in opened(picture).convert("RGB").getcolors(maxcolors=100000)).get((0, 255, 0), 0), 1500)

    # ---- animation ----

    def animated(self, source: bytes, **inputs):
        made = design(width=240, height=120, background={"kind": "upload", "id": "g"}, layers=[{"type": "rect", "x": 200, "y": 80, "w": 30, "h": 30, "fill": {"kind": "color", "color": "#ffffff"}}])
        return render.render(made, Inputs(pictures={"upload:g": Raw(source, f"g{zlib.crc32(source)}")}, **inputs))

    def test_an_animated_background_gives_a_gif_with_the_layers_on_every_frame(self):
        picture = self.animated(moving_gif(frames=6, duration=80))
        self.assertEqual((picture.kind, picture.filename, picture.frames, picture.width, picture.height), ("gif", "welcome.gif", 6, 240, 120))
        image = opened(picture)
        self.assertEqual((image.format, image.n_frames, image.size), ("GIF", 6, (240, 120)))
        for index in range(6):
            image.seek(index)
            frame = image.convert("RGB")
            self.assertTrue(near(frame.getpixel((215, 95)), (255, 255, 255), 10), f"the layer is missing from frame {index}")
            self.assertTrue(near(frame.getpixel((230, 20)), frame_color(index), 14), f"frame {index} holds the wrong background")
            self.assertEqual(image.info["duration"], 80)

    def test_frames_stored_as_changes_come_out_whole(self):
        source = drifting_gif(frames=5)
        with Image.open(io.BytesIO(source)) as stored:
            stored.seek(3)
            left, top, right, bottom = stored.tile[0][1]
            self.assertLess((right - left) * (bottom - top), 120 * 60 // 4, "this sample has to store a later frame as a small patch")
        image = opened(self.animated(source))
        image.seek(3)
        frame = image.convert("RGB")
        blue, yellow = (20, 60, 160), (255, 255, 0)
        self.assertTrue(near(frame.getpixel((2 * (5 + 3 * 20) + 20, 60)), yellow, 14), "the square is where the fourth frame puts it")
        for earlier in range(3):
            self.assertTrue(near(frame.getpixel((2 * (5 + earlier * 20) + 20, 60)), blue, 14), "no trail is left where the square was")
        self.assertTrue(near(frame.getpixel((120, 110)), blue, 14), "the part of the frame the patch never touched is still there")

    def test_a_background_frame_disposed_to_background_is_cleared_and_not_painted_the_files_background_color(self):
        source = hand_gif((120, 60), [{"index": 1}, {"box": (10, 10, 30, 20), "index": 2, "disposal": 2}, {"box": (70, 30, 30, 20), "index": 3}])
        image = opened(self.animated(source))
        image.seek(2)
        frame = image.convert("RGB")
        self.assertTrue(near(frame.getpixel((50, 40)), (0, 0, 0), 14), f"the cleared box shows the black under every animation, not white: {frame.getpixel((50, 40))}")
        self.assertTrue(near(frame.getpixel((170, 80)), BLUE, 14))
        self.assertTrue(near(frame.getpixel((20, 100)), RED, 14))

    def test_a_background_whose_first_frame_is_smaller_than_the_gif_shows_nothing_around_it(self):
        source = hand_gif((120, 60), [{"box": (40, 20, 40, 20), "index": 5}, {"box": (0, 0, 20, 20), "index": 2}], colors=[RED] + GIF_COLORS[1:])
        moving = opened(self.animated(source)).convert("RGB")
        self.assertTrue(near(moving.getpixel((20, 100)), (0, 0, 0), 14), f"black, not the palette's first color: {moving.getpixel((20, 100))}")
        still = opened(self.animated(source, animate=False)).convert("RGBA")
        self.assertEqual(still.getpixel((20, 100))[3], 0, "the still picture is see-through around the first frame")
        self.assertTrue(near(still.getpixel((120, 60)), YELLOW, 14))

    def test_a_background_frame_that_names_no_disposal_keeps_its_own(self):
        source = hand_gif((120, 60), [{"index": 1}, {"box": (10, 10, 30, 20), "index": 2, "disposal": 2}, {"box": (50, 10, 20, 20), "index": 3}, {"box": (80, 30, 20, 20), "index": 5}])
        image = opened(self.animated(source))
        image.seek(3)
        frame = image.convert("RGB")
        self.assertTrue(near(frame.getpixel((120, 40)), BLUE, 14), f"the blue box stays in the last frame: {frame.getpixel((120, 40))}")

    def test_an_animated_png_background_moves_as_pillow_lays_it(self):
        made = [Image.new("RGB", (120, 60), frame_color(index)) for index in range(3)]
        out = io.BytesIO()
        made[0].save(out, format="PNG", save_all=True, append_images=made[1:], duration=[60, 70, 80], loop=0)
        image = opened(self.animated(out.getvalue()))
        self.assertEqual(image.n_frames, 3)
        for index, length in enumerate((60, 70, 80)):
            image.seek(index)
            self.assertTrue(near(image.convert("RGB").getpixel((20, 20)), frame_color(index), 14), f"frame {index}")
            self.assertEqual(image.info["duration"], length)

    def test_timing_and_looping_are_kept(self):
        image = opened(self.animated(moving_gif(frames=4, duration=[50, 120, 200, 90], loop=3)))
        times = []
        for index in range(4):
            image.seek(index)
            times.append(image.info["duration"])
        self.assertEqual(times, [50, 120, 200, 90])
        self.assertEqual(image.info.get("loop"), 3)
        self.assertEqual(opened(self.animated(moving_gif(frames=4, loop=0))).info.get("loop"), 0)
        self.assertNotIn("loop", opened(self.animated(moving_gif(frames=4, loop=None))).info, "a GIF that plays once still plays once")

    def test_with_animation_off_the_first_frame_is_the_background(self):
        picture = self.animated(moving_gif(), animate=False)
        self.assertEqual(picture.kind, "png")
        self.assertTrue(near(opened(picture).convert("RGB").getpixel((230, 20)), frame_color(0), 8))

    def test_a_still_upload_gives_a_png(self):
        self.assertEqual(self.animated(png(120, 60)).kind, "png")

    def test_too_many_frames_are_thinned_and_the_pace_stays(self):
        with mock.patch.object(render, "MAX_FRAMES", 5):
            picture = self.animated(moving_gif(frames=20, duration=40))
        self.assertEqual(picture.frames, 5)
        image = opened(picture)
        total = 0
        for index in range(image.n_frames):
            image.seek(index)
            total += image.info["duration"]
        self.assertEqual(total, 20 * 40)
        self.assertIn("The background has 20 frames. This picture's size leaves room for 5, so some are skipped and the pace stays the same.", picture.notes)

    def test_frames_times_pixels_are_bounded(self):
        with mock.patch.object(render, "MAX_FRAME_PIXELS", 240 * 120 * 3):
            self.assertEqual(self.animated(moving_gif(frames=9)).frames, 3)
        with mock.patch.object(render, "MAX_FRAME_PIXELS", 240 * 120):
            self.assertEqual(self.animated(moving_gif(frames=9)).kind, "png", "room for one frame means a still picture")

    def test_an_animation_over_the_byte_limit_is_thinned_then_shrunk_then_made_still(self):
        source = moving_gif(240, 120, frames=32)
        full = self.animated(source, max_bytes=10_000_000)
        self.assertEqual(full.frames, 32)
        thinner = self.animated(source, max_bytes=len(full.data) * 2 // 3)
        self.assertEqual((thinner.kind, thinner.frames, thinner.width), ("gif", 16, 240))
        self.assertLessEqual(len(thinner.data), len(full.data) * 2 // 3)
        self.assertIn("The animation was thinned to 16 frames to stay under the size limit for one file.", thinner.notes)
        with mock.patch.object(render, "_gif", side_effect=lambda frames, lengths, loop: (b"x" * (9000 if frames[0].width == 240 else 100), len(frames))):
            smaller = self.animated(source, max_bytes=5000)
        self.assertEqual((smaller.kind, smaller.width, smaller.frames), ("gif", 180, 8))
        self.assertIn("The animation was scaled down to stay under the size limit for one file.", smaller.notes)
        still = self.animated(source, max_bytes=700)  # less than a GIF's color table alone
        self.assertEqual(still.kind, "png")
        self.assertLessEqual(len(still.data), 700)
        self.assertIn("The animation is too heavy to send, so the picture is a still one.", still.notes)

    def test_a_picture_too_heavy_even_when_small_is_refused(self):
        with self.assertRaises(render.RenderError):
            render.render(design(background={"kind": "stock", "name": "confetti"}), Inputs(max_bytes=300))

    def test_a_heavy_still_picture_is_scaled_down_to_fit(self):
        made = design(background={"kind": "stock", "name": "confetti"})
        full = render.render(made, Inputs())
        smaller = render.render(made, Inputs(max_bytes=len(full.data) * 3 // 4))
        self.assertLess(smaller.width, 400)
        self.assertLessEqual(len(smaller.data), len(full.data) * 3 // 4)
        self.assertEqual(smaller.notes, ["The picture was scaled down to stay under the size limit for one file."])

    def test_an_animation_that_runs_out_of_time_falls_back_to_a_still_picture(self):
        clock = [100.0]
        calls = [0]

        def later() -> float:
            calls[0] += 1
            if calls[0] > 14:  # the clock jumps once the frames are being put together
                clock[0] = 100.0 + render.ANIMATED_SECONDS + 0.5
            return clock[0]
        with mock.patch.object(render.time, "monotonic", later):
            picture = self.animated(moving_gif(frames=10))
        self.assertEqual(picture.kind, "png")
        self.assertIn("The animation took too long to draw, so the picture is a still one.", picture.notes)

    def slow_frames(self, seconds: float = 0.1):
        """Every frame of a GIF takes this long to read."""
        from PIL import GifImagePlugin
        real = GifImagePlugin.GifImageFile.seek

        def slow(image, frame):
            time.sleep(seconds)
            return real(image, frame)
        return mock.patch.object(GifImagePlugin.GifImageFile, "seek", slow)

    def test_a_background_that_takes_too_long_to_read_gives_a_still_picture_in_the_time_given(self):
        slow_note = "The background's animation took too long to read, so the picture is a still one."
        for limits in ({"seconds": 2.0}, {}):
            started = time.monotonic()
            # The renderer's own limits, made short: an animation has longer than a still, as it does at full length.
            with self.slow_frames(), mock.patch.object(render, "ANIMATED_SECONDS", 2.0), mock.patch.object(render, "STILL_SECONDS", 1.0):
                picture = self.animated(moving_gif(frames=40), **limits)
            took = time.monotonic() - started
            self.assertEqual(picture.kind, "png", limits)
            self.assertIn(slow_note, picture.notes)
            self.assertLess(took, 2.0 + 0.5, f"the still is ready inside the time the animation had: {took:.2f} s")

    def test_a_cached_animation_with_little_time_left_still_gives_a_picture(self):
        source = moving_gif(frames=10)
        self.animated(source)  # the fitted frames are now cached, so nothing is read
        for seconds in (1.5, 3.0):
            started = time.monotonic()
            picture = self.animated(source, seconds=seconds)
            self.assertLess(time.monotonic() - started, seconds)
            self.assertEqual((picture.kind, picture.frames), ("gif", 10), f"with {seconds} s left the cached animation is drawn whole")

    def test_layers_that_run_out_of_time_with_the_frames_in_hand_give_a_still(self):
        source = moving_gif(frames=10)
        self.animated(source)
        real = render._Draw.overlay
        calls = []

        def slow(work):
            calls.append(work)
            time.sleep(1.2)  # every time: the layers take longer than the animation has left, and as long again if drawn again
            return real(work)
        started = time.monotonic()
        with mock.patch.object(render._Draw, "overlay", slow):
            picture = self.animated(source, seconds=2.0)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(picture.kind, "png")
        self.assertIn("The animation took too long to draw, so the picture is a still one.", picture.notes)
        self.assertEqual(len(calls), 1, "the layers are drawn once, for the animation and for the still")
        # With no limit from the caller, the layers have the animation's whole time.
        with mock.patch.object(render._Draw, "overlay", slow), mock.patch.object(render, "ANIMATED_SECONDS", 3.0), mock.patch.object(render, "STILL_SECONDS", 1.0):
            self.assertEqual(self.animated(source).kind, "gif")

    def test_an_animation_out_of_time_past_the_callers_limit_is_refused(self):
        source = moving_gif(frames=10)
        self.animated(source)
        clock = [100.0]
        real = render._composed

        def composed_late(work, frame, overlay):
            clock[0] = 105.5  # the clock passes the caller's limit while the frames are put together
            return real(work, frame, overlay)
        with mock.patch.object(render.time, "monotonic", lambda: clock[0]), mock.patch.object(render, "_composed", composed_late), self.assertRaises(render.RenderError) as refused:
            self.animated(source, seconds=5.0)
        self.assertEqual(str(refused.exception), "The picture took too long to draw. Use a shorter or smaller animation.")

    def test_a_slow_read_that_ends_in_time_and_slow_layers_still_give_a_picture(self):
        real = render._Draw.overlay

        def slow_layers(work):
            time.sleep(0.8)
            return real(work)
        started = time.monotonic()
        with self.slow_frames(), mock.patch.object(render._Draw, "overlay", slow_layers):
            picture = self.animated(moving_gif(frames=12), seconds=4.0)  # 1.2 s of reading, inside the 2 s the read has
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertEqual(picture.kind, "gif", "the layers end after the read's time but well inside the caller's")

    def test_when_even_the_still_runs_out_of_time_the_refusal_names_the_background(self):
        real = render._Draw.overlay

        def slow_layers(work):
            time.sleep(render.GRACE_SECONDS + 0.3)
            return real(work)
        with self.slow_frames(), mock.patch.object(render._Draw, "overlay", slow_layers), self.assertRaises(render.RenderError) as refused:
            self.animated(moving_gif(frames=40), seconds=3.0)
        self.assertEqual(str(refused.exception), "The animated background took too long to read. Use a shorter or smaller animation.")

    def test_an_animation_out_of_time_long_past_its_grace_is_refused(self):
        clock = [100.0]
        calls = [0]

        def later() -> float:
            calls[0] += 1
            if calls[0] > 14:  # the clock jumps once the frames are being put together, past the grace a still picture has
                clock[0] = 100.0 + render.ANIMATED_SECONDS + render.GRACE_SECONDS + 1
            return clock[0]
        with mock.patch.object(render.time, "monotonic", later), self.assertRaises(render.RenderError) as refused:
            self.animated(moving_gif(frames=10))
        self.assertEqual(str(refused.exception), "The picture took too long to draw. Use a shorter or smaller animation.")

    def test_a_gif_is_read_only_as_far_as_the_frame_cap(self):
        with mock.patch.object(render, "MAX_SOURCE_FRAMES", 4):
            picture = self.animated(moving_gif(frames=10))
        self.assertEqual((picture.kind, picture.frames), ("gif", 4))

    def test_a_picture_the_upload_checks_refuse_is_never_decoded(self):
        made = design(width=240, height=120, background={"kind": "upload", "id": "g"})
        too_wide = png(5000, 64, color=(255, 0, 255))
        picture = render.render(made, Inputs(pictures={"upload:g": Raw(too_wide, "too-wide")}))
        self.assertFalse(near(opened(picture).convert("RGB").getpixel((5, 5)), (255, 0, 255)))
        self.assertIn("An uploaded picture this design names is gone, so the avatar's color stands in.", picture.notes)

    def test_a_draw_that_is_out_of_time_before_it_has_a_picture_is_refused(self):
        made = design(layers=[{"type": "rect", "x": 0, "y": 0, "w": 10, "h": 10}])
        with self.assertRaises(render.RenderError) as refused:
            render.render(made, Inputs(seconds=-1))
        self.assertIn("took too long", str(refused.exception))

    def gray_gif(self, frames: list[tuple[int, int, bool]]) -> bytes:
        """A 200x100 animation of flat grays, each (gray, milliseconds, whether a light patch sits in its top left corner). Fitted to a
        canvas wider than 2 by 1, the top rows are cropped away, patch and all."""
        made = []
        for gray, _, patch in frames:
            image = Image.new("RGB", (200, 100), (gray, gray, gray))
            if patch:
                ImageDraw.Draw(image).rectangle((0, 0, 19, 9), fill=(250, 250, 250))
            made.append(image)
        out = io.BytesIO()
        made[0].save(out, format="GIF", save_all=True, append_images=made[1:], duration=[length for _, length, _ in frames], loop=0)
        return out.getvalue()

    def wide_animated(self, source: bytes) -> render.Picture:
        """An animated background on a canvas three times as wide as it is tall, so a 200x100 frame loses its top and bottom rows."""
        made = design(width=300, height=100, background={"kind": "upload", "id": "g"}, layers=[{"type": "rect", "x": 260, "y": 60, "w": 30, "h": 30}])
        return render.render(made, Inputs(pictures={"upload:g": Raw(source, f"w{zlib.crc32(source)}")}))

    def lengths_in(self, picture: render.Picture) -> list[int]:
        image = opened(picture)
        out = []
        for index in range(getattr(image, "n_frames", 1)):
            image.seek(index)
            out.append(image.info.get("duration"))
        return out

    def test_frames_that_come_out_alike_and_last_long_draw_as_a_still(self):
        source = self.gray_gif([(90, 400_000, False), (90, 400_000, True)])
        self.assertEqual(Image.open(io.BytesIO(source)).n_frames, 2)
        picture = self.wide_animated(source)
        self.assertEqual((picture.kind, picture.frames), ("png", 1), "nothing moves once the background is fitted")
        self.assertEqual(picture.notes, ["The background's frames look the same once it is fitted to the picture, so the picture is a still one."])

    def test_no_frame_lasts_longer_than_a_minute(self):
        picture = self.wide_animated(self.gray_gif([(index * 4, 655_350, False) for index in range(20)]))
        self.assertEqual(picture.kind, "gif")
        self.assertTrue(all(length <= render.LONGEST_FRAME for length in self.lengths_in(picture)), self.lengths_in(picture))

    def test_the_frame_count_is_what_the_file_holds(self):
        picture = self.wide_animated(self.gray_gif([(90, 4000, False), (90, 4000, True), (150, 4000, False)]))
        self.assertEqual((picture.kind, picture.frames), ("gif", 2))
        self.assertEqual(opened(picture).n_frames, 2)
        self.assertEqual(self.lengths_in(picture), [8000, 4000], "the frame that came out alike adds its time to the one before it")

    def test_an_animation_the_gif_writer_refuses_draws_as_a_still(self):
        for error in (OverflowError("unsigned short is greater than maximum"), ValueError("bad frame")):
            with mock.patch.object(render, "_gif", side_effect=error):
                picture = self.animated(moving_gif(frames=6))
            self.assertEqual(picture.kind, "png")
            self.assertIn("The animation couldn't be written, so the picture is a still one.", picture.notes)

    def test_sixty_fitted_frames_on_the_wide_layout_are_kept_between_draws(self):
        render.CACHE.clear()
        made = model.clean_design({"width": 1000, "height": 300, "background": {"kind": "upload", "id": "g"}, "layers": [
            {"type": "text", "x": 300, "y": 32, "w": 668, "h": 236, "text": "Welcome"}]}, fonts.KEYS)
        source = moving_gif(480, 270, frames=60)
        inputs = lambda: Inputs(pictures={"upload:g": Raw(source, "sixty")})  # noqa: E731
        first = render.render(made, inputs())
        self.assertEqual((first.kind, first.frames), ("gif", 60))
        kept = render.CACHE.get(("frames", "sixty", 1000, 300, 60))
        self.assertIsNotNone(kept, "the fitted frames are kept for the next join")
        with mock.patch.object(render, "_open", side_effect=AssertionError("the upload was read and fitted again")):
            self.assertEqual(render.render(made, inputs()).data, first.data)

    def test_the_largest_frame_set_the_limits_allow_fits_in_the_cache(self):
        render.CACHE.clear()
        self.animated(moving_gif(frames=4))
        frames, _, _ = next(value for key, (value, _) in render.CACHE.items.items() if key[0] == "frames")
        self.assertEqual({frame.mode for frame in frames}, {"P"}, "fitted frames are kept at one byte a pixel")
        self.assertLessEqual(render.MAX_FRAME_PIXELS + render.MAX_FRAMES * 768, render.CACHE.limit // 2, "the most frames of the most pixels fit under the cache's limit for one item")

    def test_a_cached_animation_draws_the_same_gif(self):
        source = moving_gif(frames=5)
        first = self.animated(source)
        self.assertIsNotNone(render.CACHE.get(("frames", f"g{zlib.crc32(source)}", 240, 120, render.MAX_FRAMES)))
        with mock.patch.object(render, "_open", side_effect=AssertionError("the upload was decoded again")):
            self.assertEqual(self.animated(source).data, first.data)

    # ---- the cache ----

    def test_the_cache_stays_under_its_limit_and_drops_the_oldest_first(self):
        cache = render._Cache(1000)
        cache.put(("a",), "first", 400)
        cache.put(("b",), "second", 400)
        self.assertEqual(cache.get(("a",)), "first")  # reading it makes it the newest
        cache.put(("c",), "third", 400)
        self.assertEqual((cache.get(("a",)), cache.get(("b",)), cache.get(("c",))), ("first", None, "third"))
        self.assertLessEqual(cache.held, 1000)
        cache.put(("huge",), "too big to keep", 600)
        self.assertIsNone(cache.get(("huge",)))

    def test_an_avatar_is_never_kept_in_the_cache(self):
        made = design(layers=[{"type": "avatar", "x": 0, "y": 0, "w": 100, "h": 100}])
        render.render(made, Inputs(avatar=Raw(face_png())))
        self.assertEqual(list(render.CACHE.items), [])


class GifFramesTest(unittest.TestCase):
    """A GIF's frames are laid over each other the way browsers show them. In each case here Pillow lays them some other way."""

    SCREEN = (120, 60)
    FIRST, SECOND, THIRD = (10, 10, 30, 20), (50, 20, 30, 20), (80, 30, 30, 20)

    def shown(self, data: bytes) -> list[Image.Image]:
        return [canvas.copy() for _, canvas in gif.composed(gif.parse(data, 100))]

    def over_red(self, disposal: int) -> list[Image.Image]:
        """A red frame, a green box that names `disposal`, and a blue box beside it."""
        return self.shown(hand_gif(self.SCREEN, [{"index": 1}, {"box": self.FIRST, "index": 2, "disposal": disposal}, {"box": self.SECOND, "index": 3}]))

    def test_a_frame_disposed_to_background_is_cleared_to_see_through(self):
        frames = self.over_red(2)
        self.assertEqual(frames[1].getpixel((20, 15)), (*GREEN, 255))
        self.assertEqual(frames[2].getpixel((20, 15)), (0, 0, 0, 0), "cleared, not painted the white the file names as its background")
        self.assertEqual(frames[2].getpixel((60, 25)), (*BLUE, 255))
        self.assertEqual(frames[2].getpixel((5, 50)), (*RED, 255))

    def test_disposal_3_and_4_put_back_what_was_under_and_every_other_value_keeps_the_frame(self):
        for disposal, after in ((0, GREEN), (1, GREEN), (3, RED), (4, RED), (5, GREEN), (6, GREEN), (7, GREEN)):
            self.assertEqual(self.over_red(disposal)[2].getpixel((20, 15)), (*after, 255), f"disposal {disposal}")

    def test_a_frame_that_names_no_disposal_never_takes_the_one_before_it(self):
        for plain in (False, True):  # a control block that names none, or no control block at all
            frames = self.shown(hand_gif(self.SCREEN, [{"index": 1}, {"box": self.FIRST, "index": 2, "disposal": 2}, {"box": self.SECOND, "index": 3, "plain": plain}, {"box": self.THIRD, "index": 5}]))
            self.assertEqual(frames[3].getpixel((55, 25)), (*BLUE, 255), f"no control block: {plain}")
        frames = self.shown(hand_gif(self.SCREEN, [{"index": 1}, {"box": self.FIRST, "index": 5, "clear": 5}, {"box": self.SECOND, "index": 5, "plain": True}]))
        self.assertEqual(frames[2].getpixel((55, 25)), (*YELLOW, 255), "nor its transparent color")

    def test_a_frames_transparent_color_shows_what_is_under_it(self):
        frames = self.shown(hand_gif(self.SCREEN, [{"index": 1}, {"box": self.FIRST, "index": 2, "clear": 2}, {"box": self.SECOND, "index": 3, "clear": 5}]))
        self.assertEqual(frames[1].getpixel((20, 15)), (*RED, 255), "the green box is all transparent color")
        self.assertEqual(frames[2].getpixel((60, 25)), (*BLUE, 255), "a transparent color the frame doesn't use hides nothing")

    def test_a_first_frame_smaller_than_the_gif_leaves_the_rest_see_through(self):
        data = hand_gif(self.SCREEN, [{"box": (40, 20, 40, 20), "index": 5}, {"box": self.FIRST, "index": 2}], colors=[RED] + GIF_COLORS[1:])
        for first in (self.shown(data)[0], gif.first(data)):
            self.assertEqual(first.getpixel((5, 5)), (0, 0, 0, 0), "not the palette's first color")
            self.assertEqual(first.getpixel((50, 30)), (*YELLOW, 255))

    def test_a_first_frame_past_the_edge_grows_the_picture_and_later_frames_are_cut_at_it(self):
        parsed = gif.parse(hand_gif(self.SCREEN, [{"box": (100, 40, 60, 30), "index": 1}, {"box": (110, 50, 60, 30), "index": 2}]), 100)
        self.assertEqual((parsed.width, parsed.height), (160, 70))
        frames = [canvas.copy() for _, canvas in gif.composed(parsed)]
        self.assertEqual((frames[0].getpixel((155, 65)), frames[0].getpixel((5, 5))), ((*RED, 255), (0, 0, 0, 0)))
        self.assertEqual(frames[1].getpixel((155, 65)), (*GREEN, 255))
        later = gif.parse(hand_gif(self.SCREEN, [{"index": 1}, {"box": (100, 40, 60, 30), "index": 2}]), 100)
        self.assertEqual((later.width, later.height), self.SCREEN, "only the first frame grows the picture")
        with self.assertRaises(ValueError):
            gif.parse(hand_gif(self.SCREEN, [{"box": (4000, 0, 120, 60), "index": 1}]), 100)  # grown wider than any picture the bot reads

    def test_a_byte_that_starts_no_block_ends_the_file(self):
        frames = [{"index": 1}, {"box": self.FIRST, "index": 2}, {"box": self.SECOND, "index": 3, "before": bytes(1)}, {"box": self.THIRD, "index": 5}]
        parsed = gif.parse(hand_gif(self.SCREEN, frames), 100)
        self.assertEqual((len(parsed.frames), parsed.whole), (2, True))
        self.assertEqual(gif.count(hand_gif(self.SCREEN, frames)), 2)
        self.assertEqual(len(gif.parse(hand_gif(self.SCREEN, frames[:2], tail=bytes(4)), 100).frames), 2, "zeros where the trailer should be")

    def test_a_file_that_ends_inside_a_frame_keeps_the_frames_before_it_and_says_it_is_not_whole(self):
        data = hand_gif(self.SCREEN, [{"index": 1}, {"box": self.FIRST, "index": 2}, {"box": self.SECOND, "index": 3}])
        parsed = gif.parse(data[:-40], 100)
        self.assertEqual((len(parsed.frames), parsed.whole), (2, False))
        self.assertTrue(gif.parse(data, 100).whole)

    def test_a_file_cut_inside_a_block_after_its_last_frame_has_lost_no_frame(self):
        for tail in (b"!", b"!" + bytes([0xFE]), b"!" + bytes([0xFE, 20]) + b"cut short"):
            parsed = gif.parse(hand_gif(self.SCREEN, [{"index": 1}, {"box": self.FIRST, "index": 2}], tail=tail), 100)
            self.assertEqual((len(parsed.frames), parsed.whole), (2, True), f"it reads as a file that ends there, as one without a trailer does: {tail}")

    def test_a_frame_without_colors_is_drawn_black_as_a_browser_shows_it(self):
        bare = hand_gif(self.SCREEN, [{"index": 1}, {"box": self.FIRST, "index": 2}], colors=None)
        parsed = gif.parse(bare, 100)
        self.assertEqual((len(parsed.frames), parsed.whole), (2, True))
        frames = self.shown(bare)
        self.assertEqual((frames[0].getpixel((5, 5)), frames[1].getpixel((20, 15))), ((0, 0, 0, 255), (0, 0, 0, 255)))
        mixed = hand_gif(self.SCREEN, [{"index": 1, "local": GIF_COLORS}, {"box": self.FIRST, "index": 2, "clear": 3}], colors=None)
        frames = self.shown(mixed)
        self.assertEqual((frames[0].getpixel((5, 5)), frames[1].getpixel((20, 15))), ((*RED, 255), (0, 0, 0, 255)), "only the frame without colors is black")

    def test_a_file_of_more_blocks_than_the_reader_walks_stops_and_says_so(self):
        comments = (b"!" + bytes([0xFE, 0])) * 60  # empty comment blocks, which a hostile file can hold by the million
        data = hand_gif(self.SCREEN, [{"index": 1}, {"box": self.FIRST, "index": 2, "before": comments}])
        self.assertFalse(gif.parse(data, 100).crowded)
        with mock.patch.object(gif, "MAX_PIECES", 50):
            parsed = gif.parse(data, 100)
            self.assertEqual((len(parsed.frames), parsed.whole, parsed.crowded), (1, False, True))
            self.assertEqual(gif.counted(data), (1, False))
            with self.assertRaises(gif.Crowded):
                gif.parse(hand_gif(self.SCREEN, [{"index": 1, "before": comments}]), 100)  # stopped before the first frame
        self.assertEqual(gif.counted(data), (2, True))

    def test_a_frame_whose_code_size_is_under_two_ends_the_read(self):
        one_bit = lzw_codes([2, 1] * 600 + [3], width=2)  # code size 1: a clear code before each pixel keeps every code two bits wide
        for code, data in ((0, None), (1, one_bit)):  # the GIF specification starts at code size 2, and browsers refuse a frame below it
            second = {"box": self.FIRST, "index": 1, "code": code, **({"data": data} if data else {})}
            parsed = gif.parse(hand_gif(self.SCREEN, [{"index": 1}, second, {"box": self.SECOND, "index": 3}]), 100)
            self.assertEqual((len(parsed.frames), parsed.whole), (1, False), f"code size {code}")
        self.assertTrue(gif.parse(hand_gif(self.SCREEN, [{"index": 1}, {"box": self.FIRST, "index": 1, "code": 2, "data": lzw_codes([256, 257])}]), 100).whole)

    def test_a_frame_larger_than_the_picture_ends_the_read(self):
        parsed = gif.parse(hand_gif(self.SCREEN, [{"index": 1}, {"box": (0, 0, 121, 10), "index": 2}, {"box": self.FIRST, "index": 3}]), 100)
        self.assertEqual((len(parsed.frames), parsed.whole), (1, False))

    def test_a_frame_of_no_pixels_counts_and_draws_nothing(self):
        frames = self.shown(hand_gif(self.SCREEN, [{"index": 1}, {"box": (10, 10, 0, 0), "index": 2, "disposal": 2}, {"box": self.SECOND, "index": 3}]))
        self.assertEqual(len(frames), 3)
        self.assertEqual(frames[1].getpixel((10, 10)), (*RED, 255))
        self.assertEqual(frames[2].getpixel((10, 10)), (*RED, 255))

    def test_the_loop_count_counts_wherever_it_stands_and_under_either_name(self):
        two = [{"index": 1}, {"box": self.FIRST, "index": 2}]
        self.assertIsNone(gif.parse(hand_gif(self.SCREEN, two, loop=None), 100).loop, "a GIF without one plays once")
        self.assertEqual(gif.parse(hand_gif(self.SCREEN, two, loop=3), 100).loop, 3)
        for name in (b"NETSCAPE2.0", b"ANIMEXTS1.0"):
            late = hand_gif(self.SCREEN, [two[0], {**two[1], "before": loop_block(0, name)}], loop=None)
            self.assertEqual(gif.parse(late, 100).loop, 0, name)

    def test_only_as_many_frames_as_asked_for_are_read(self):
        data = hand_gif(self.SCREEN, [{"index": index % 5 + 1} for index in range(7)])
        self.assertEqual((len(gif.parse(data, 3).frames), gif.count(data)), (3, 7))


def one_line(written: str, size: int = 400, width: int = 400, height: int = 200, **layer) -> dict:
    """A design of one text layer that keeps its whole text on one line at full size, however long."""
    return design(layers=[{"type": "text", "x": 0, "y": 0, "w": width, "h": height, "text": written, "size": size, "wrap": False, "shrink": False, **layer}])


class OneLayerLimitsTest(unittest.TestCase):
    """The limits on time and memory hold inside one text layer: what lies past the layer's picture is never drawn."""

    def largest_masks(self, made: dict) -> list[tuple[int, int]]:
        """The size of every glyph mask the fonts were asked for while the design was drawn."""
        sizes = []
        real = ImageFont.FreeTypeFont.getmask2

        def measured(font, *args, **kwargs):
            found = real(font, *args, **kwargs)
            sizes.append(found[0].size)
            return found
        with mock.patch.object(ImageFont.FreeTypeFont, "getmask2", measured):
            render.render(made, Inputs())
        return sizes

    def test_a_slanted_line_far_longer_than_its_picture_asks_for_no_image_much_wider(self):
        largest = [(0, 0)]
        real = Image.new

        def counted(mode, size, *args, **kwargs):
            if size[0] * size[1] > largest[0][0] * largest[0][1]:
                largest[0] = tuple(size)
            if size[0] * size[1] > 60_000_000:
                raise MemoryError(f"a {size[0]}x{size[1]} image was asked for")
            return real(mode, size, *args, **kwargs)
        made = one_line("*" + "W" * 998 + "*", stroke={"color": "#000000", "width": 32})
        with mock.patch.object(text.Image, "new", counted):
            render.render(made, Inputs())
        # The layer's own picture is the box with room around it, 400 + 2 x 194 wide. A tile a few sizes wider than that is the most it may take.
        self.assertLess(largest[0][0], 400 + 2 * 194 + 6 * 400, f"one 400x200 layer asked for a {largest[0][0]}x{largest[0][1]} image")

    def test_a_slanted_line_with_one_leftward_step_asks_for_no_image_much_wider(self):
        # A zero-width space and a soft hyphen have no advance in DejaVu, so letter spacing below zero steps back over them.
        for written, size, spacing in (("*W" + chr(0x200B) + "W" * 400 + "*", 400, -20), ("*Welcome" + chr(0x00AD) + "W" * 700 + "*", 200, -2)):
            largest = [(0, 0)]
            real = Image.new

            def counted(mode, size_asked, *args, **kwargs):
                if size_asked[0] * size_asked[1] > largest[0][0] * largest[0][1]:
                    largest[0] = tuple(size_asked)
                if size_asked[0] * size_asked[1] > 60_000_000:
                    raise MemoryError(f"a {size_asked[0]}x{size_asked[1]} image was asked for")
                return real(mode, size_asked, *args, **kwargs)
            made = one_line(written, size=size, font="dejavu", letter_spacing=spacing, stroke={"color": "#000000", "width": 32})
            with mock.patch.object(text.Image, "new", counted):
                render.render(made, Inputs())
            pad = round(size * 0.4) + 2 + 32
            self.assertLess(largest[0][0], 400 + 2 * pad + 6 * size, f"size {size}: one 400x200 layer asked for a {largest[0][0]}x{largest[0][1]} image")

    def test_an_upright_line_with_one_leftward_step_draws_only_the_letters_that_show(self):
        real = text._write
        drawn = []

        def counted(*args, **kwargs):
            drawn.append(args[2])
            return real(*args, **kwargs)
        made = one_line("W" + chr(0x200B) + "W" * 400, font="dejavu", letter_spacing=-20, stroke={"color": "#000000", "width": 32})
        with mock.patch.object(text, "_write", counted):
            render.render(made, Inputs())
        self.assertTrue(drawn)
        self.assertLess(len(drawn), 40, f"{len(drawn)} letters drawn for a 400x200 picture, ink and outline")

    def test_an_upright_line_far_longer_than_its_picture_asks_the_font_for_no_mask_much_wider(self):
        sizes = self.largest_masks(one_line("W" * 1000, stroke={"color": "#000000", "width": 32}))
        self.assertTrue(sizes)
        self.assertLess(max(width for width, _ in sizes), 400 + 2 * 194 + 6 * 400, "the mask of the whole line is never built")

    def test_emoji_past_the_picture_are_not_drawn(self):
        chosen = text.Setting(size=100, wrap=False, shrink=False)
        found = text.layout(M("<:dot:123456789012345678>" * 200), chosen, 400, 200)
        asked = []

        def picture(emoji):
            asked.append(emoji)
            return Image.new("RGBA", (64, 64), (255, 0, 0, 255))
        _, _, color = text.paint(found, chosen, 400, 200, 42, picture)
        self.assertIsNotNone(color.getbbox())
        self.assertLessEqual(len(asked), 12, f"{len(asked)} of 200 emoji were drawn where about six show")

    def test_lines_below_the_picture_are_not_drawn(self):
        sizes = self.largest_masks(one_line("\n".join(["Wide line"] * 40), size=100))
        self.assertLessEqual(len(sizes), 6, f"{len(sizes)} lines were drawn where two or three show")

    def test_a_long_bold_line_of_wide_characters_draws(self):
        picture = render.render(one_line("**" + chr(0xFDFD) * 996 + "**"), Inputs())
        self.assertEqual((picture.width, picture.height), (400, 200))

    def test_lines_far_longer_than_their_picture_draw_quickly(self):
        for made in (one_line("*" + "W" * 998 + "*", width=2000, height=2000, stroke={"color": "#000000", "width": 64}),
                     one_line("W" * 1000, width=2000, height=2000, stroke={"color": "#000000", "width": 64}),
                     one_line("*" + "W" * 998 + "*", letter_spacing=100, width=2000, height=2000)):
            started = time.monotonic()
            render.render(made, Inputs())
            self.assertLess(time.monotonic() - started, render.STILL_SECONDS / 3)

    def test_a_text_layer_stops_when_the_time_is_up(self):
        real = ImageDraw.ImageDraw.text

        def slow(pen, *args, **kwargs):
            time.sleep(0.02)
            return real(pen, *args, **kwargs)
        made = design(width=2000, height=2000, layers=[{"type": "text", "x": 0, "y": 0, "w": 2000, "h": 2000, "text": "a" * 1000, "size": 20, "letter_spacing": 1, "shrink": False}])
        started = time.monotonic()
        with mock.patch.object(ImageDraw.ImageDraw, "text", slow), self.assertRaises(render.RenderError):
            render.render(made, Inputs(seconds=1.0))
        self.assertLess(time.monotonic() - started, 3.0, "a thousand letters drawn one at a time stop when the time is up, not after the layer")

    def test_a_text_layer_of_many_pieces_stops_when_the_time_is_up(self):
        real = text._fitted

        def slow(*args, **kwargs):
            time.sleep(0.02)
            return real(*args, **kwargs)
        faces = chr(0x1F600) * 600  # every emoji is a piece of its own, drawn with no letter spacing and no check of its own
        made = design(width=2000, height=2000, layers=[{"type": "text", "x": 0, "y": 0, "w": 2000, "h": 2000, "text": "{x}", "size": 20, "shrink": False}])
        started = time.monotonic()
        with mock.patch.object(text, "_fitted", slow), self.assertRaises(render.RenderError):
            render.render(made, Inputs(texts={made["layers"][0]["id"]: M(faces)}, seconds=1.0))
        self.assertLess(time.monotonic() - started, 3.0, "six hundred emoji stop when the time is up, not after the layer")

    def assert_same_visible_part(self, written: str, align: str, **setting):
        """The layer's picture with the line cut to what shows is the same as the part of the whole line that falls in that picture."""
        chosen = text.Setting(**{"size": 40, "outline": 3, "align": align, "valign": "top", "wrap": False, "shrink": False, **setting})
        found = text.layout(M(written), chosen, 300, 100)
        ends = [end for line in found.lines for placed in line.placed for end in (placed.x, placed.x + placed.width)]  # spacing below zero runs a piece leftward
        whole = max(ends) - min(ends)
        self.assertGreater(whole, 1200, "the line is far wider than its picture")
        ink, outline, _ = text.paint(found, chosen, 300, 100, 20, lambda emoji: None)
        lead = math.ceil(1000 - min(ends))  # whole pixels, so every letter keeps its place within a pixel
        for line in found.lines:
            for placed in line.placed:
                placed.x += lead
        wide_ink, wide_outline, _ = text.paint(found, chosen, round(lead + whole + 1000), 100, 20, lambda emoji: None)
        for cut, uncut in ((ink, wide_ink), (outline, wide_outline)):
            reference = uncut.crop((lead, 0, lead + cut.width, cut.height))
            self.assertIsNotNone(reference.getbbox(), "some of the line shows")
            differs = ImageChops.difference(cut, reference)
            self.assertLessEqual(differs.getextrema()[1], 40, f"{written[:12]!r} aligned {align}: the cut line is drawn where the whole line puts it")
            self.assertLess(sum(differs.histogram()[16:]), 20)

    def test_a_line_cut_on_either_side_is_drawn_where_the_whole_line_puts_it(self):
        kerned = "AVATAR WAVE Today VA " * 12
        for align in ("left", "right", "center"):
            self.assert_same_visible_part(kerned, align)
            self.assert_same_visible_part("*" + kerned + "*", align)
            self.assert_same_visible_part(kerned, align, letter_spacing=3)
            self.assert_same_visible_part(chr(0x05E9) + chr(0x05DC) + chr(0x05D5) + chr(0x05DD) + " " + (chr(0x05E2) + chr(0x05D5) + chr(0x05DC) + chr(0x05DD) + " ") * 30, align, family="dejavu")

    def test_a_line_letter_spacing_runs_leftward_keeps_the_part_in_the_picture(self):
        for align in ("left", "right", "center"):
            self.assert_same_visible_part("i" * 300, align, size=12, letter_spacing=-20)
            self.assert_same_visible_part("i i " * 80, align, size=10, letter_spacing=-20)
            self.assert_same_visible_part("WWWWiiii" * 100, align, size=30, letter_spacing=-20)  # some letters step right and some left
            self.assert_same_visible_part("*" + "i" * 300 + "*", align, size=12, letter_spacing=-20)  # slanted, on a tile cut to what shows
            self.assert_same_visible_part("*" + "W" * 150 + chr(0x200B) + "W" * 150 + "*", align, size=40, letter_spacing=-20, family="dejavu")


COLORS24 = [f"#{index * 10:02x}80ff" for index in range(24)]


class DesignsThatSaveDrawTest(unittest.TestCase):
    """A design the save checks accept is drawn, or refused with a RenderError that says why. Nothing else comes out of render."""

    def draws(self, *layers: dict) -> render.Picture:
        try:
            return render.render(design(layers=list(layers)), Inputs(person=3 << 22))
        except render.RenderError as error:
            self.fail(f"refused: {error}")

    def test_stripes_with_more_colors_than_the_layer_has_pixels(self):
        self.draws({"type": "rect", "x": 10, "y": 10, "w": 10, "h": 100, "fill": {"kind": "stripes", "direction": "vertical", "colors": COLORS24}})
        self.draws({"type": "rect", "x": 10, "y": 10, "w": 100, "h": 3, "fill": {"kind": "stripes", "direction": "horizontal", "colors": COLORS24[:5]}})
        self.draws({"type": "ellipse", "x": 10, "y": 10, "w": 1, "h": 100, "fill": {"kind": "stripes", "direction": "vertical", "colors": COLORS24[:2]}})

    def test_stripes_too_many_for_their_band_keep_every_pixel_filled(self):
        drawn = backgrounds.stripes(5, 4, COLORS24, "vertical")
        self.assertEqual(drawn.size, (5, 4))
        self.assertEqual(drawn.getpixel((4, 3))[:3], (230, 128, 255), "the last color reaches the far edge")
        self.assertEqual({drawn.getpixel((x, 0))[3] for x in range(5)}, {255})

    def test_corners_that_scale_to_under_a_pixel(self):
        self.draws({"type": "rect", "x": 10, "y": 10, "w": 200, "h": 60, "radius": [1000, 1000, 4, 4]})
        self.draws({"type": "picture", "x": 10, "y": 10, "w": 100, "h": 100, "source": {"kind": "stock", "name": "dusk"}, "radius": [1, 400, 0, 0]})
        self.draws({"type": "avatar", "x": 10, "y": 10, "w": 200, "h": 200, "shape": "rounded", "radius": [1, 700, 0, 0]})
        self.draws({"type": "rect", "x": 10, "y": 10, "w": 120, "h": 120, "radius": [40, 1000, 0, 0], "stroke": {"color": "#ffffff", "width": 39}})

    def test_slanted_letters_pulled_together_past_their_width_draw(self):
        for written, size in (("*iiiii*", 12), ("*" + "i" * 30 + "*", 40), ("*" + "WWWWiiii" * 4 + "*", 30)):
            for align in ("left", "right", "center"):
                drawn = self.draws({"type": "text", "x": 50, "y": 50, "w": 300, "h": 100, "text": written, "size": size, "letter_spacing": -20, "align": align})
                lit = sum(1 for pixel in opened(drawn).convert("L").getdata() if pixel > 128)
                self.assertGreater(lit, 0, f"{written[:10]!r} at {size}, aligned {align}: the slanted letters in the layer's box show")
        boxes = []
        for written in ("*" + "i" * 6 + "*", "i" * 6):  # slanted and upright, run leftward from the right edge
            drawn = self.draws({"type": "text", "x": 50, "y": 50, "w": 300, "h": 100, "text": written, "size": 20, "letter_spacing": -20, "align": "right"})
            boxes.append(opened(drawn).convert("L").point(lambda value: 255 if value > 128 else 0).getbbox())
        self.assertTrue(abs(boxes[0][0] - boxes[1][0]) <= 6 and abs(boxes[0][2] - boxes[1][2]) <= 6, f"the slanted letters lie where the upright ones do: {boxes}")

    def test_underlined_letters_pulled_together_past_their_width(self):
        drawn = self.draws({"type": "text", "x": 0, "y": 0, "w": 400, "h": 200, "text": "__il i l__", "size": 40, "letter_spacing": -20, "stroke": {"color": "#000000", "width": 4}})
        self.assertEqual((drawn.width, drawn.height), (400, 200))

    def test_a_letter_under_a_tower_of_marks_draws(self):
        tower = "a" + chr(0x0300) * 998
        sizes = []
        real = ImageFont.FreeTypeFont.getmask2

        def measured(font, *args, **kwargs):
            found = real(font, *args, **kwargs)
            sizes.append(found[0].size)
            return found
        with mock.patch.object(ImageFont.FreeTypeFont, "getmask2", measured):
            drawn = self.draws({"type": "text", "x": 0, "y": 0, "w": 3, "h": 1, "text": tower, "font": "dejavu", "size": 400, "wrap": False, "shrink": False,
                                "stroke": {"color": "#000000", "width": 64}})
        self.assertEqual((drawn.width, drawn.height), (400, 200))
        self.assertLess(max(width * height for width, height in sizes), 10_000_000, "the marks a font stacks up are not drawn a thousand high")
        def named(written: str) -> text.Marked:  # as a filled template notes a name: its plain letters came from outside
            return text.Marked(written, bytes(1 if " " <= character <= "~" else 0 for character in written))
        kept = text.parse(named("a" + chr(0x0300) * 200 + " " + "a" + chr(0x0300) * 200))[0].text
        self.assertEqual(kept, chr(0x00E0) + chr(0x0300) * text.MAX_MARKS + " " + chr(0x00E0) + chr(0x0300) * text.MAX_MARKS,
                         "the first mark makes the letter's composed form, and at most 30 more stay on it, as in Unicode's stream-safe text")
        self.assertEqual(text.parse(named("e" + chr(0x0323) + chr(0x0302) + chr(0x0300)))[0].text, chr(0x1EC7) + chr(0x0300), "ordinary marks stay")
        self.assertEqual(text.parse(M("a" + chr(0x0300) * 200))[0].text, "a" + chr(0x0300) * text.MAX_MARKS, "the server's own letters keep their marks as written, 30 at most")

    def test_every_mix_of_small_and_large_corners_draws(self):
        sizes = (0, 1, 2, 3, 4, 8, 16, 33, 1000)
        for first in sizes:
            for second in sizes:
                for stroke in (0, 3):
                    self.draws({"type": "rect", "x": 0, "y": 0, "w": 160, "h": 70, "radius": [first, second, first // 2, 1000 if second else 0],
                                "stroke": {"color": "#ffffff", "width": stroke}})

    def test_what_the_drawing_libraries_refuse_comes_out_as_a_sentence(self):
        for error in (ValueError("x1 must be greater than or equal to x0"), OverflowError("unsigned short is greater than maximum"), MemoryError(),
                      Image.DecompressionBombError("Image size exceeds limit")):
            with mock.patch.object(render._Draw, "overlay", side_effect=error), self.assertRaises(render.RenderError) as refused:
                render.render(design(), Inputs())
            self.assertEqual(str(refused.exception), render.UNDRAWABLE)


class LimitsTest(unittest.TestCase):
    def test_the_largest_canvas_draws_within_its_time(self):
        made = model.clean_design({"width": 2000, "height": 2000, "background": {"kind": "stock", "name": "mesh"}, "layers": [
            {"type": "avatar", "x": 700, "y": 200, "w": 600, "h": 600, "shape": "hexagon", "stroke": {"color": "#ffffff", "width": 24}},
            {"type": "text", "x": 200, "y": 1000, "w": 1600, "h": 800, "text": "Welcome", "size": 300, "align": "center", "stroke": {"color": "#000000", "width": 12}}]}, fonts.KEYS)
        started = time.monotonic()
        picture = render.render(made, Inputs())
        self.assertEqual((picture.width, picture.height), (2000, 2000))
        self.assertLess(time.monotonic() - started, render.STILL_SECONDS)
        self.assertLess(len(picture.data), render.DEFAULT_MAX_BYTES)


if __name__ == "__main__":
    unittest.main()
