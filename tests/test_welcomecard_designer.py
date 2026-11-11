"""The welcome picture's design as data: what a design may hold, what the ready-made layouts build, and how a text is filled in."""
from __future__ import annotations

import datetime
import unittest
from types import SimpleNamespace

from captainguild.core.tagscript import Interpreter, Scope
from captainguild.core.validate import Invalid
from captainguild.core.welcomecard import fonts, layouts, model, template, text

NOW = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)


def clean(**parts) -> dict:
    return model.clean_design({"width": 400, "height": 200, **parts}, fonts.KEYS)


def person(name: str = "Sam", user_id: int = 5) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, name=name.lower(), display_name=name, mention=f"<@{user_id}>", bot=False, roles=[], top_role=None, created_at=NOW, joined_at=NOW,
                           display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/5/abc.png?size=1024"))


def server(name: str = "Harbor") -> SimpleNamespace:
    return SimpleNamespace(id=9, name=name, icon=None, member_count=8102, owner=None, owner_id=1, members=[], roles=[], channels=[], created_at=NOW)


def scope(name: str = "Sam", server_name: str = "Harbor") -> Scope:
    return Scope(guild=server(server_name), member=person(name))


class ColorTest(unittest.TestCase):
    def test_the_forms_a_color_can_take(self):
        for written, kept in (("#FF8800", "#ff8800"), ("ff8800", "#ff8800"), ("#f80", "#ff8800"), (0xFF8800, "#ff8800"), ("#ff880080", "#ff880080"), ("#ff8800ff", "#ff8800"),
                              ("rgb(255, 136, 0)", "#ff8800"), ("rgba(255, 136, 0, 0.5)", "#ff880080"), ("rgba(255, 136, 0, 128)", "#ff880080"), ("rgba(255,136,0,1)", "#ff8800"), (" #ff8800 ", "#ff8800")):
            self.assertEqual(model.color(written), kept, written)

    def test_what_is_not_a_color_is_refused_with_a_sentence(self):
        for written in ("", "red", "#ff88", "#gggggg", "rgb(300, 0, 0)", "rgba(1, 2, 3, 300)", None, True, -1, 0x1000000, 1.5, ["#ffffff"]):
            with self.assertRaises(Invalid, msg=repr(written)) as refused:
                model.color(written)
            self.assertIn("six hex digits", str(refused.exception))

    def test_a_color_reads_back_as_four_numbers(self):
        self.assertEqual(model.rgba("#ff8800"), (255, 136, 0, 255))
        self.assertEqual(model.rgba("#ff880080"), (255, 136, 0, 128))


class FillTest(unittest.TestCase):
    def test_each_kind_of_fill_comes_out_in_one_shape(self):
        self.assertEqual(model.clean_fill({"kind": "none"}), {"kind": "none"})
        self.assertEqual(model.clean_fill({"kind": "avatar"}), {"kind": "avatar"})
        self.assertEqual(model.clean_fill({"kind": "color", "color": "FFF"}), {"kind": "color", "color": "#ffffff"})
        self.assertEqual(model.clean_fill({"kind": "gradient", "colors": ["#000", "#fff"]}), {"kind": "gradient", "angle": 90, "colors": ["#000000", "#ffffff"]})
        self.assertEqual(model.clean_fill({"kind": "stripes", "direction": "vertical", "colors": ["#000"]}), {"kind": "stripes", "direction": "vertical", "colors": ["#000000"]})
        self.assertEqual(model.clean_fill({"kind": "stock", "name": "aurora"}), {"kind": "stock", "name": "aurora"})
        self.assertEqual(model.clean_fill({"kind": "upload", "id": "0123abcd"}), {"kind": "upload", "id": "0123abcd"})

    def test_stripes_with_no_colors_are_refused_at_save(self):
        with self.assertRaises(Invalid) as refused:
            model.clean_fill({"kind": "stripes", "colors": []})
        self.assertEqual(str(refused.exception), "Stripes need at least 1 color.")
        with self.assertRaises(Invalid) as refused:
            model.clean_fill({"kind": "gradient", "colors": ["#fff"]})
        self.assertEqual(str(refused.exception), "Gradients need at least 2 colors.")

    def test_too_many_colors_names_the_limit(self):
        with self.assertRaises(Invalid) as refused:
            model.clean_fill({"kind": "stripes", "colors": ["#fff"] * 25})
        self.assertEqual(str(refused.exception), "Stripes can hold at most 24 colors. This has 25.")

    def test_a_fill_takes_no_field_it_has_no_use_for(self):
        for bad in ({"kind": "color", "color": "#fff", "url": "https://example.com/x.png"}, {"kind": "none", "color": "#fff"}, {"kind": "url", "url": "x"}, {"kind": "stock", "name": "../etc"},
                    {"kind": "upload", "id": 7}, {"kind": "upload", "id": "A B"}, "solid:#fff", None, []):
            with self.assertRaises(Invalid, msg=repr(bad)):
                model.clean_fill(bad)


class DesignTest(unittest.TestCase):
    def test_an_empty_design_gets_every_default(self):
        self.assertEqual(model.clean_design({}, fonts.KEYS), {"v": 1, "width": 1000, "height": 300, "background": {"kind": "avatar"}, "frame": {"color": "#ffffff", "width": 0}, "layers": []})

    def test_a_layer_needs_only_its_type(self):
        made = clean(layers=[{"type": "text"}, {"type": "picture"}, {"type": "avatar"}, {"type": "rect"}, {"type": "ellipse"}])
        text_layer, picture, avatar, rect, ellipse = made["layers"]
        self.assertEqual([layer["id"] for layer in made["layers"]], ["l1", "l2", "l3", "l4", "l5"])
        self.assertEqual({key: text_layer[key] for key in ("font", "weight", "size", "line_height", "letter_spacing", "align", "valign", "wrap", "shrink", "text")},
                         {"font": "fredoka", "weight": 400, "size": 48, "line_height": 1.2, "letter_spacing": 0, "align": "left", "valign": "top", "wrap": True, "shrink": True, "text": ""})
        self.assertEqual((picture["source"], picture["fit"], picture["radius"]), ({"kind": "address", "address": ""}, "cover", [0, 0, 0, 0]))
        self.assertEqual((avatar["shape"], avatar["stroke"]), ("circle", {"color": "#ffffff", "width": 0}))
        self.assertEqual((rect["x"], rect["y"], rect["w"], rect["h"], rect["rotation"], rect["flip_x"], rect["flip_y"], rect["opacity"], rect["hidden"]), (0, 0, 200, 100, 0, False, False, 100, False))
        self.assertNotIn("radius", ellipse)
        self.assertNotIn("text", rect)

    def test_cleaning_twice_changes_nothing(self):
        made = layouts.build({**layouts.DEFAULTS, "layout": "split", "frame": True})
        self.assertEqual(model.clean_design(made, fonts.KEYS), made)

    def test_the_canvas_limits(self):
        for width, height in ((99, 200), (2001, 200), (400, 99), (400, 2001), ("wide", 200), (float("nan"), 200), (True, 200)):
            with self.assertRaises(Invalid, msg=(width, height)) as refused:
                model.clean_design({"width": width, "height": height}, fonts.KEYS)
            self.assertIn("The canvas can be 100 to 2,000 pixels on a side.", str(refused.exception))
        self.assertEqual((clean(width=100, height=2000)["width"], clean(width=100, height=2000)["height"]), (100, 2000))

    def test_the_canvas_refusal_says_which_side_and_how_big(self):
        with self.assertRaises(Invalid) as refused:
            model.clean_design({"width": 2400, "height": 300}, fonts.KEYS)
        self.assertEqual(str(refused.exception), "The canvas can be 100 to 2,000 pixels on a side. Its width is 2,400.")

    def test_forty_layers_and_no_more(self):
        self.assertEqual(len(clean(layers=[{"type": "rect", "w": 10, "h": 10}] * 40)["layers"]), 40)
        with self.assertRaises(Invalid) as refused:
            clean(layers=[{"type": "rect", "w": 10, "h": 10}] * 41)
        self.assertEqual(str(refused.exception), "A design can hold at most 40 layers. This one has 41.")

    def test_the_layers_together_have_a_pixel_limit(self):
        with self.assertRaises(Invalid) as refused:
            clean(layers=[{"type": "rect", "w": 2000, "h": 2000}] * 7)
        self.assertEqual(str(refused.exception), "The layers cover 28.0 million pixels together. The limit is 24 million, so make some smaller or remove one.")
        self.assertEqual(len(clean(layers=[{"type": "rect", "w": 2000, "h": 2000}] * 6)["layers"]), 6)

    def test_text_and_address_lengths(self):
        self.assertEqual(len(clean(layers=[{"type": "text", "text": "x" * 1000}])["layers"][0]["text"]), 1000)
        with self.assertRaises(Invalid) as refused:
            clean(layers=[{"type": "text", "name": "Title", "text": "x" * 1001}])
        self.assertEqual(str(refused.exception), "Layer 1 (Title)'s text can be at most 1,000 characters. This is 1,001.")
        with self.assertRaises(Invalid) as refused:
            clean(layers=[{"type": "rect"}, {"type": "picture", "source": {"kind": "address", "address": "x" * 301}}])
        self.assertEqual(str(refused.exception), "Layer 2's picture's address can be at most 300 characters. This is 301.")

    def test_numbers_out_of_range_name_the_field_and_the_range(self):
        cases = (
            ({"type": "rect", "w": 0}, "Layer 1's width can be 1 to 2,000. This is 0."),
            ({"type": "rect", "h": 2001}, "Layer 1's height can be 1 to 2,000. This is 2,001."),
            ({"type": "rect", "x": 5000}, "Layer 1's left edge can be -4,000 to 4,000. This is 5,000."),
            ({"type": "rect", "opacity": 101}, "Layer 1's opacity can be 0 to 100. This is 101."),
            ({"type": "rect", "rotation": 400}, "Layer 1's rotation can be -360 to 360. This is 400."),
            ({"type": "rect", "stroke": {"width": 65}}, "Layer 1's stroke's width can be 0 to 64. This is 65."),
            ({"type": "text", "size": 5}, "Layer 1's size can be 6 to 400. This is 5."),
            ({"type": "text", "weight": 1000}, "Layer 1's weight can be 100 to 900. This is 1,000."),
            ({"type": "text", "line_height": 0.2}, "Layer 1's line height can be 0.6 to 3. This is 0.2."),
            ({"type": "rect", "radius": [0, 0, 0, 1001]}, "Layer 1's corners can be 0 to 1,000. This is 1,001."),
        )
        for layer, sentence in cases:
            with self.assertRaises(Invalid, msg=layer) as refused:
                clean(layers=[layer])
            self.assertEqual(str(refused.exception), sentence)
        with self.assertRaises(Invalid) as refused:
            clean(frame={"color": "#fff", "width": 65})
        self.assertEqual(str(refused.exception), "The frame's width can be 0 to 64. This is 65.")

    def test_wrong_kinds_of_values_are_refused(self):
        for layer in ({"type": "star"}, {"type": "rect", "hidden": "yes"}, {"type": "rect", "x": "10"}, {"type": "rect", "radius": [1, 2]}, {"type": "text", "font": "comic-sans"}, {"type": "text", "align": "justify"},
                      {"type": "avatar", "shape": "star"}, {"type": "picture", "fit": "tile"}, {"type": "picture", "source": {"kind": "color", "color": "#fff"}}, {"type": "rect", "id": "Has Space"},
                      {"type": "rect", "href": "https://example.com"}, {"type": "ellipse", "radius": 5}, {"type": "rect", "text": "words"}, "rect", None):
            with self.assertRaises(Invalid, msg=repr(layer)):
                clean(layers=[layer])
        for bad in ({"layers": "none"}, {"v": 2}, {"extra": 1}, []):
            with self.assertRaises(Invalid, msg=repr(bad)):
                model.clean_design(bad, fonts.KEYS)

    def test_a_field_nobody_knows_is_named_in_the_refusal(self):
        with self.assertRaises(Invalid) as refused:
            clean(layers=[{"type": "rect", "glow": 4}])
        self.assertEqual(str(refused.exception), "Layer 1 has no setting called glow.")

    def test_two_layers_never_share_an_id(self):
        made = clean(layers=[{"type": "rect", "id": "a"}, {"type": "rect", "id": "a"}, {"type": "rect"}, {"type": "rect", "id": "l1"}])
        ids = [layer["id"] for layer in made["layers"]]
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual((ids[0], ids[3]), ("a", "l1"))

    def test_one_number_sets_all_four_corners(self):
        self.assertEqual(clean(layers=[{"type": "rect", "radius": 12}])["layers"][0]["radius"], [12, 12, 12, 12])

    def test_numbers_are_rounded_to_what_is_stored(self):
        layer = clean(layers=[{"type": "text", "x": 10.4, "w": 99.6, "rotation": 12.345, "size": 33.33, "letter_spacing": 1.26}])["layers"][0]
        self.assertEqual((layer["x"], layer["w"], layer["rotation"], layer["size"], layer["letter_spacing"]), (10, 100, 12.35, 33.3, 1.3))

    def test_the_uploads_a_design_names(self):
        made = clean(background={"kind": "upload", "id": "aaa"}, layers=[
            {"type": "rect", "fill": {"kind": "upload", "id": "bbb"}}, {"type": "picture", "source": {"kind": "upload", "id": "ccc"}, "hidden": True}, {"type": "picture", "source": {"kind": "stock", "name": "hills"}}])
        self.assertEqual(model.uploads_in(made), {"aaa", "bbb", "ccc"})
        self.assertEqual(model.uploads_in(clean()), set())

    def test_whether_a_design_needs_the_avatar(self):
        self.assertTrue(model.uses_avatar(clean()))  # the default background is the avatar's color
        self.assertFalse(model.uses_avatar(clean(background={"kind": "none"}, layers=[{"type": "rect"}, {"type": "avatar", "hidden": True}])))
        self.assertTrue(model.uses_avatar(clean(background={"kind": "none"}, layers=[{"type": "avatar"}])))
        self.assertTrue(model.uses_avatar(clean(background={"kind": "none"}, layers=[{"type": "text", "fill": {"kind": "avatar"}}])))


class LayoutsTest(unittest.TestCase):
    def built(self, **settings) -> dict:
        return layouts.build({**layouts.DEFAULTS, **settings})

    def layer(self, made: dict, identity: str) -> dict:
        return next(layer for layer in made["layers"] if layer["id"] == identity)

    def box(self, made: dict, identity: str) -> tuple[int, int, int, int]:
        layer = self.layer(made, identity)
        return layer["x"], layer["y"], layer["w"], layer["h"]

    def test_there_are_seven_and_three_keep_the_long_used_canvas_sizes(self):
        sizes = {row["key"]: (row["width"], row["height"]) for row in layouts.catalog()}
        self.assertEqual((sizes["wide"], sizes["tall"], sizes["badge"]), ((1000, 300), (750, 516), (964, 320)))
        self.assertGreaterEqual(len(sizes), 6)
        self.assertEqual(tuple(sizes), layouts.KEYS)
        for key in layouts.KEYS:
            made = self.built(layout=key)
            self.assertEqual((made["width"], made["height"]), sizes[key], key)

    def test_wide_puts_the_avatar_left_and_the_text_right(self):
        made = self.built(layout="wide")
        self.assertEqual(self.box(made, "avatar"), (32, 32, 236, 236))
        self.assertEqual(self.box(made, "text"), (300, 32, 668, 236))
        self.assertEqual(self.box(self.built(layout="wide", avatar=False), "text"), (32, 32, 936, 236))

    def test_tall_puts_the_avatar_top_center_and_the_text_below(self):
        made = self.built(layout="tall")
        self.assertEqual(self.box(made, "avatar"), (257, 32, 236, 236))
        x, y, w, h = self.box(made, "text")
        self.assertEqual((x, w), (32, 686))
        self.assertGreaterEqual(y, 268)
        self.assertLessEqual(y + h, 516 - 32)

    def test_badge_is_a_rounded_panel_with_the_avatar_over_its_left_end(self):
        made = self.built(layout="badge")
        self.assertEqual([layer["id"] for layer in made["layers"]], ["panel", "avatar", "text"], "the avatar is drawn after the panel, so it sits on top")
        self.assertEqual(self.box(made, "panel"), (82, 65, 850, 190))
        self.assertEqual(self.layer(made, "panel")["radius"], [32, 32, 32, 32])
        self.assertEqual(self.box(made, "avatar"), (32, 32, 256, 256))
        self.assertEqual(self.box(made, "text")[:2], (320, 82))
        bare = self.built(layout="badge", avatar=False)
        self.assertEqual(self.box(bare, "panel"), (32, 65, 900, 190))

    def test_every_layout_draws_with_and_without_the_avatar(self):
        for key in layouts.KEYS:
            for avatar in (True, False):
                made = self.built(layout=key, avatar=avatar)
                self.assertEqual(any(layer["type"] == "avatar" for layer in made["layers"]), avatar, key)
                boxes = [layer for layer in made["layers"] if layer["type"] in ("text", "avatar")]
                for layer in boxes:
                    self.assertGreaterEqual(layer["x"], 0, (key, layer["id"]))
                    self.assertLessEqual(layer["x"] + layer["w"], made["width"], (key, layer["id"]))
                    self.assertLessEqual(layer["y"] + layer["h"], made["height"], (key, layer["id"]))

    def test_the_simple_settings_reach_the_design(self):
        made = self.built(text="Hi {user}", text_position="bottom_right", text_color="#112233", outline_color="#445566", outline_width=7, font="lora", avatar_shape="hexagon", ring_color="#778899",
                          ring_width=12, background={"kind": "stripes", "direction": "vertical", "colors": ["#000000", "#ffffff"]})
        words, avatar = self.layer(made, "text"), self.layer(made, "avatar")
        self.assertEqual((words["text"], words["align"], words["valign"], words["font"], words["weight"]), ("Hi {user}", "right", "bottom", "lora", fonts.FAMILIES["lora"].display))
        self.assertEqual((words["fill"], words["stroke"]), ({"kind": "color", "color": "#112233"}, {"color": "#445566", "width": 7}))
        self.assertTrue(words["wrap"] and words["shrink"])
        self.assertEqual((avatar["shape"], avatar["stroke"]), ("hexagon", {"color": "#778899", "width": 12}))
        self.assertEqual(made["background"], {"kind": "stripes", "direction": "vertical", "colors": ["#000000", "#ffffff"]})

    def test_the_nine_text_positions(self):
        self.assertEqual(len(layouts.POSITIONS), 9)
        self.assertEqual(set(layouts.POSITIONS.values()), {(a, v) for a in ("left", "center", "right") for v in ("top", "middle", "bottom")})
        for position, (align, valign) in layouts.POSITIONS.items():
            words = self.layer(self.built(text_position=position), "text")
            self.assertEqual((words["align"], words["valign"]), (align, valign), position)

    def test_left_to_the_layout_the_text_sits_where_that_layout_reads_best(self):
        self.assertEqual(layouts.DEFAULTS["text_position"], "auto")
        self.assertEqual(layouts.TEXT_POSITIONS[0], "auto")
        self.assertEqual(set(layouts.TEXT_POSITIONS[1:]), set(layouts.POSITIONS))
        wanted = {"wide": "left", "badge": "left", "split": "left", "strip": "left", "tall": "center", "banner": "center", "spotlight": "center"}
        self.assertEqual({row["key"]: row["position"] for row in layouts.catalog()}, wanted)
        for key, align in wanted.items():
            for avatar in (True, False):
                words = self.layer(self.built(layout=key, text_position="auto", avatar=avatar), "text")
                self.assertEqual((words["align"], words["valign"]), (align, "middle"), (key, avatar))

    def test_a_chosen_text_position_applies_in_every_layout(self):
        for key in layouts.KEYS:
            for position, placed in layouts.POSITIONS.items():
                words = self.layer(self.built(layout=key, text_position=position), "text")
                self.assertEqual((words["align"], words["valign"]), placed, (key, position))

    def test_the_frame_is_off_until_it_is_switched_on(self):
        self.assertEqual(self.built()["frame"]["width"], 0)
        self.assertEqual(self.built(frame=True, frame_color="#abcdef", frame_width=20)["frame"], {"color": "#abcdef", "width": 20})

    def test_the_defaults_name_things_that_exist(self):
        from captainguild.core.welcomecard import backgrounds
        self.assertIn(layouts.DEFAULTS["layout"], layouts.LAYOUTS)
        self.assertIn(layouts.DEFAULTS["font"], fonts.FAMILIES)
        self.assertIn(layouts.DEFAULTS["background"]["name"], backgrounds.CATALOG)
        self.assertIn(layouts.DEFAULTS["text_position"], layouts.TEXT_POSITIONS)
        self.assertIn(layouts.DEFAULTS["avatar_shape"], layouts.SHAPES)


class TemplateTest(unittest.TestCase):
    def filled(self, source: str, name: str = "Sam", server_name: str = "Harbor") -> text.Marked:
        return template.fill_text(Interpreter(scope(name, server_name)), source)

    def spans(self, source: str, name: str = "Sam", server_name: str = "Harbor"):
        return [(span.text, span.bold, span.italic, span.underline, span.emoji) for span in text.parse(self.filled(source, name, server_name))]

    def test_a_text_is_filled_in_by_the_template_language(self):
        marked = self.filled("Welcome {user} to {server}, member {ord:{server(members)}}")
        self.assertEqual(marked.text, "Welcome Sam to Harbor, member 8,102nd")
        self.assertTrue(marked.marks)

    def test_what_came_from_outside_is_noted_character_by_character(self):
        marked = self.filled("Hi {user}!")
        self.assertEqual(marked.text, "Hi Sam!")
        self.assertEqual(marked.outside, bytes([0, 0, 0, 1, 1, 1, 0]))

    def test_the_servers_marks_work_around_a_name(self):
        self.assertEqual(self.spans("Welcome **{user}**"), [("Welcome ", False, False, False, ""), ("Sam", True, False, False, "")])

    def test_a_name_can_not_make_itself_bold_italic_or_underlined(self):
        self.assertEqual(self.spans("Welcome {user}!", name="**Sam**"), [("Welcome **Sam**!", False, False, False, "")])
        self.assertEqual(self.spans("Welcome {user}!", name="*x* __y__"), [("Welcome *x* __y__!", False, False, False, "")])
        self.assertEqual(self.spans("{upper:{user}}", name="**Sam**"), [("**SAM**", False, False, False, "")])
        self.assertEqual(self.spans("to {server}", server_name="__HQ__"), [("to __HQ__", False, False, False, "")])

    def test_a_name_can_not_bring_a_server_emoji_with_it(self):
        self.assertEqual(self.spans("{user} joined", name="<:evil:123456789012345678>"), [("<:evil:123456789012345678> joined", False, False, False, "")])
        self.assertEqual(self.spans("{user} <:wave:223456789012345678>"), [("Sam ", False, False, False, ""), ("", False, False, False, "223456789012345678")])

    def test_a_name_that_starts_with_a_mark_leaves_what_the_server_wrote_before_it_whole(self):
        overlay = chr(0x0338)  # COMBINING LONG SOLIDUS OVERLAY, as zalgo names carry
        emoji = "<:wave:123456789012345678>"
        self.assertEqual([span[4] for span in self.spans(emoji + "{user}", name=overlay + "Sam") if span[4]], ["123456789012345678"])
        zalgo = "".join(chr(0x0300 + index) for index in range(32))  # a name of marks alone
        self.assertEqual([span[4] for span in self.spans(emoji + "{user}" + emoji, name=zalgo) if span[4]], ["123456789012345678"] * 2)
        for before, mark in (("=", overlay), ("<", overlay), ("a", chr(0x0301))):
            tidied = text._tidy(self.filled("x" + before + "{user}", name=mark + "Sam")).text
            self.assertTrue(tidied.startswith("x" + before + mark), f"{before!r} took the name's mark: {tidied!r}")

    def test_a_name_typed_with_separate_marks_is_composed_through_the_template(self):
        tidied = text._tidy(self.filled("Hi {user}!", name="Vie" + chr(0x0302) + chr(0x0323) + "t"))
        self.assertEqual(tidied.text, "Hi Vi" + chr(0x1EC7) + "t!")

    def test_a_name_with_a_bracket_and_a_parenthesis_is_drawn_as_written(self):
        # Posts guard a name's "](" with a backslash, so it can't make a link. A picture has no links, so the name is drawn as the member wrote it.
        marked = self.filled("Welcome **{user}**", name="[TAG](EU) Sam")
        self.assertEqual(marked.text, "Welcome **[TAG](EU) Sam**")
        self.assertEqual(marked.outside, bytes([0] * 10 + [1] * 13 + [0] * 2), "the outside notes stay in step")
        self.assertEqual(self.filled("{user}", name="a] (b").text, "a] (b")
        self.assertEqual(self.filled("[x]\\(y) {user}").text, self.filled("[x]\\(y)").text + " Sam", "what the server wrote is left as the engine gives it")

    def test_a_name_can_not_close_or_open_a_block(self):
        self.assertEqual(self.filled("Hi {user}", name="a}b{c").text, "Hi a}b{c")
        self.assertEqual(self.filled("{if(1==1):yes {user}|no}", name="|no}").text, "yes |no}")

    def test_a_brace_with_no_partner_stays_as_written(self):
        self.assertEqual(self.filled("smile :} {user} and { open").text, "smile :} Sam and { open")
        self.assertEqual(self.filled("} {user} {").text, "} Sam {")
        self.assertEqual(self.filled("{{user}").text, "{Sam")

    def test_a_block_nothing_answers_to_stays_as_written(self):
        self.assertEqual(self.filled("{nothing} {user}").text, "{nothing} Sam")

    def test_a_variable_set_in_one_text_is_there_for_the_next(self):
        run = Interpreter(scope())
        self.assertEqual(template.fill_text(run, "{=(greeting):Ahoy}{greeting} {user}").text, "Ahoy Sam")
        self.assertEqual(template.fill_text(run, "{greeting} again").text, "Ahoy again")

    def test_conditions_and_marks_inside_a_block(self):
        self.assertEqual(self.spans("{if({server(members)}>100):**big** {user}|small}"), [("big", True, False, False, ""), (" Sam", False, False, False, "")])

    def test_a_break_block_keeps_its_marks_and_its_notes(self):
        self.assertEqual(self.spans("before {break(1==1):only **{user}**} after", name="*S*"), [("only ", False, False, False, ""), ("*S*", True, False, False, "")])

    def test_a_stop_block_ends_the_text_and_nothing_in_it_is_a_mark(self):
        marked = self.filled("first {stop(1==1):halted {user}} **never**", name="**Sam**")
        self.assertEqual(marked.text, "first halted **Sam**")
        self.assertFalse(marked.marks)

    def test_text_with_no_blocks_is_taken_as_written(self):
        marked = self.filled("  **Just** this  ")
        self.assertEqual((marked.text, marked.outside, marked.marks), ("**Just** this", bytes(13), True))

    def test_a_text_that_asks_for_too_much_is_drawn_as_written(self):
        marked = self.filled("{user}" + "{=(a):{a}{a}}" * 2 + "x" * 100_001)
        self.assertTrue(marked.text.startswith("{user}"))
        self.assertEqual(sum(marked.outside), 0)

    def test_the_holding_variable_is_gone_after_each_text(self):
        run = Interpreter(scope())
        template.fill_text(run, "Hi {user}")
        self.assertNotIn("welcomecard", run.variables)
        self.assertEqual(template.fill_text(run, "{welcomecard}").text, "{welcomecard}")

    def test_an_address_is_filled_in_as_plain_text(self):
        run = Interpreter(scope())
        self.assertEqual(template.fill_address(run, "{user(avatar)}"), "https://cdn.discordapp.com/avatars/5/abc.png?size=1024")
        self.assertEqual(template.fill_address(run, "{server(icon)}"), "")
        self.assertEqual(template.fill_address(run, " https://cdn.discordapp.com/icons/1/a.png "), "https://cdn.discordapp.com/icons/1/a.png")

    def test_a_whole_design_is_filled_in_layer_order(self):
        made = clean(layers=[
            {"type": "text", "id": "first", "text": "{=(n):{user}}Hello {n}"},
            {"type": "picture", "id": "face", "source": {"kind": "address", "address": "{user(avatar)}"}},
            {"type": "text", "id": "second", "text": "Bye {n}"},
            {"type": "text", "id": "unseen", "text": "{user}", "hidden": True},
            {"type": "picture", "id": "stocked", "source": {"kind": "stock", "name": "hills"}},
        ])
        filled = template.fill(made, scope())
        self.assertEqual({key: value.text for key, value in filled.texts.items()}, {"first": "Hello Sam", "second": "Bye Sam"})
        self.assertEqual(filled.addresses, {"face": "https://cdn.discordapp.com/avatars/5/abc.png?size=1024"})

    def test_the_server_emoji_the_texts_draw_are_gathered_while_they_are_filled(self):
        made = clean(layers=[
            {"type": "text", "id": "first", "text": "{user} <:wave:223456789012345678> <a:spin:323456789012345678>"},
            {"type": "text", "id": "second", "text": "<:wave:223456789012345678> \\<:no:423456789012345678> <:hat:523456789012345678>"},
            {"type": "text", "id": "unseen", "text": "<:gone:623456789012345678>", "hidden": True},
        ])
        filled = template.fill(made, scope(name="<:evil:723456789012345678>"))
        self.assertEqual(filled.emoji, ["223456789012345678", "323456789012345678", "523456789012345678"], "in order, each once, and none a name brought")
        self.assertEqual(template.Filled().emoji, [])


if __name__ == "__main__":
    unittest.main()
