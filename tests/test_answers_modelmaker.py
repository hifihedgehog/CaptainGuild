"""Printable models for /make_model: the OpenSCAD runs, held in, with stand-ins for the programs, and what each outcome tells the model."""
from __future__ import annotations

import io
import subprocess
import sys
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from captainguild.core.answers import modelmaker
from captainguild.core.answers.engine import Answer


def three_mf(points: list[tuple[float, float, float]]) -> bytes:
    vertices = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in points)
    model = f'<model><resources><object><mesh><vertices>{vertices}</vertices><triangles><triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="2" v3="3"/></triangles></mesh></object></resources></model>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("3D/3dmodel.model", model)
    return buffer.getvalue()


class OpenScad:
    """Stands in for the openscad, xvfb-run and prlimit programs, writing what each step would."""

    def __init__(self, *, fail_render: str = "", preview: bool = True, model: bytes | None = None, warnings: str = "", slow: str = ""):
        self.fail_render, self.preview, self.model, self.warnings, self.slow = fail_render, preview, model, warnings, slow
        self.commands: list[list[str]] = []

    def __call__(self, command, folder, seconds):
        self.commands.append(command)
        if self.slow and self.slow in command:
            raise subprocess.TimeoutExpired(command, seconds)
        if "model.stl" in command:
            if self.fail_render:
                return subprocess.CompletedProcess(command, 1, "", f"Parsing design\nERROR: {self.fail_render}\n")
            Path(folder, "model.stl").write_bytes(b"solid x")
            return subprocess.CompletedProcess(command, 0, "", f"{self.warnings}Rendering done\n")
        if "model.3mf" in command:
            Path(folder, "model.3mf").write_bytes(self.model if self.model is not None else three_mf([(0, 0, 0), (40, 0, 0), (40, 20, 5.5), (0, 20, 5.5)]))
            return subprocess.CompletedProcess(command, 0, "", "")
        if "preview.png" in command and self.preview:
            Path(folder, "preview.png").write_bytes(b"\x89PNG preview")
        return subprocess.CompletedProcess(command, 0, "", "")


class ModelMakerTest(unittest.IsolatedAsyncioTestCase):
    async def made(self, scad="cube([40, 20, 5.5]);", name="Cookie Coaster!", programs=None):
        programs = programs or OpenScad()
        answer = Answer(status="ok")
        with mock.patch.object(modelmaker, "run_held", programs):
            report, failed = await modelmaker.render_model({"name": name, "scad": scad}, answer)
        return answer, report, failed, programs

    async def test_a_render_gives_the_3mf_the_source_and_the_preview(self):
        answer, report, failed, programs = await self.made()
        self.assertFalse(failed, report)
        self.assertEqual(report, "Rendered. Size 40.0 x 20.0 x 5.5 mm (X by Y by Z). Lowest point z = 0.00 mm. 2 triangles.")
        self.assertEqual([name for name, _ in answer.files], ["cookie-coaster.3mf", "cookie-coaster.scad"])
        self.assertEqual(answer.files[1][1], b"cube([40, 20, 5.5]);")
        self.assertEqual((answer.image, answer.image_name, answer.image_label), (b"\x89PNG preview", "preview.png", "Model"))
        self.assertEqual(answer.image_caption, "cookie-coaster.3mf, 40.0 x 20.0 x 5.5 mm")
        self.assertEqual(answer.screen_text, ["cube([40, 20, 5.5]);"], "the whole source goes to the word screen")
        self.assertEqual(programs.commands[0][:2], ["openscad", "-o"])
        self.assertIn("--render", programs.commands[2])

    async def test_warnings_go_back_to_the_model_and_a_missing_preview_doesnt_stop_it(self):
        answer, report, failed, _ = await self.made(programs=OpenScad(preview=False, warnings="WARNING: one\nWARNING: two\n"))
        self.assertFalse(failed)
        self.assertTrue(report.endswith("OpenSCAD warnings: WARNING: one | WARNING: two"))
        self.assertIsNone(answer.image)
        _, report, failed, _ = await self.made(programs=OpenScad(slow="preview.png"))
        self.assertFalse(failed, report)

    async def test_what_goes_wrong_goes_back_to_the_model(self):
        cases = [
            ({"scad": 'include <lib.scad>\ncube(1);'}, "include and use read other files, and code here can't read files"),
            ({"scad": 'x = import("a.stl");'}, "import reads a file, and code here can't read files"),
            ({"scad": "x" * 60_001}, "The code is over 60000 characters"),
            ({"programs": OpenScad(fail_render="Parser error in line 3")}, "OpenSCAD failed:\nERROR: Parser error in line 3"),
            ({"programs": OpenScad(slow="model.stl")}, "The render took over 150 seconds"),
            ({"programs": OpenScad(slow="model.3mf")}, "Packing the model took too long"),
            ({"programs": OpenScad(model=b"x" * (modelmaker.MODEL_FILE_MAX_BYTES + 1))}, "too large to post"),
            ({"programs": OpenScad(model=three_mf([]))}, "The rendered model is empty"),
        ]
        for options, expected in cases:
            answer, report, failed, _ = await self.made(**options)
            self.assertTrue(failed, expected)
            self.assertIn(expected, report)
            self.assertEqual(answer.files, [])

    async def test_a_machine_without_the_programs(self):
        def missing(command, folder, seconds):
            raise FileNotFoundError("prlimit")
        answer, report, failed, _ = await self.made(programs=missing)
        self.assertTrue(failed)
        self.assertEqual(report, "Model rendering isn't available on this bot. Tell the member it's unavailable right now.")
        with mock.patch.object(modelmaker.shutil, "which", lambda name: None if name == "prlimit" else "/usr/bin/" + name):
            self.assertFalse(modelmaker.available())
        with mock.patch.object(modelmaker.shutil, "which", lambda name: "/usr/bin/" + name):
            self.assertTrue(modelmaker.available())

    def test_file_names(self):
        self.assertEqual(modelmaker.file_name("Big Bracket (v2)!"), "big-bracket-v2")
        self.assertEqual(modelmaker.file_name("!!!"), "model")
        self.assertEqual(len(modelmaker.file_name("a" * 99)), 40)


# Every way OpenSCAD 2021.01, the version the prompt asks for, reads a file or a path: from its source at the openscad-2021.01 tag
# (src/...) and the OpenSCAD User Manual's pages (named in quotes).
READS_FILES = [
    # src/lexer.l:142, "Include Statement": include <file> reads the file in as code, wherever the lexer meets it.
    "include <lib.scad>\ncube(1);", "include\n<lib.scad>",
    # src/lexer.l:151 and src/FileModule.cc:69, "Include Statement" and "Text": use <file> loads a library or a font.
    'use <fonts/Font.ttf>\ntext("A");', "use<lib.scad>",
    # src/import.cc:251 and :85-94, "Importing Geometry": import(file), import(file = ...) and the older filename = ..., and the
    # import() function later versions have for JSON.
    'import("a.stl");', 'import(file = "a.svg");', 'import(filename = "a.stl");', 'data = import("a.json");',
    # src/import.cc:247-249, "Importing Geometry" and RELEASE_NOTES.md:301 (2011.12): the deprecated import_stl, import_off and import_dxf.
    'import_stl("a.stl", convexity = 5);', 'import_off("a.off");', 'import_dxf(file = "a.dxf", layer = "plate");',
    # src/surface.cc:88-94 and :329, "Other Language Features": surface(file) reads a height map or a picture.
    'surface(file = "a.dat", center = true);', 'surface("a.png");',
    # src/linearextrude.cc:59 and :80-85, RELEASE_NOTES.md:299-300: file is linear_extrude's first parameter, so a string given by name,
    # or as the first unnamed argument wherever it sits in the list (src/evalcontext.cc:66), is read.
    'linear_extrude(file = "a.dxf", height = 5);', 'linear_extrude("a.dxf");', 'path = "a.dxf";\nlinear_extrude(path);',
    'linear_extrude(height = 5, "a.dxf");',
    # src/linearextrude.cc:185, RELEASE_NOTES.md:297: dxf_linear_extrude, the deprecated name for the same module.
    'dxf_linear_extrude(file = "a.dxf", height = 5);',
    # src/rotateextrude.cc:55 and :71-76, RELEASE_NOTES.md:299-300: rotate_extrude reads any value given as file, by name or unnamed.
    'rotate_extrude(file = "a.dxf");', 'rotate_extrude("a.dxf");', 'path = "a.dxf";\nrotate_extrude(path, convexity = 4);',
    # src/rotateextrude.cc:133, RELEASE_NOTES.md:297: dxf_rotate_extrude, the deprecated name for the same module.
    'dxf_rotate_extrude(file = "a.dxf");',
    # src/dxfdim.cc:63 and :249, src/dxfdim.cc:171 and :254, "General" (Getting input): the functions dxf_dim and dxf_cross read a DXF.
    'width = dxf_dim(file = "a.dxf", name = "width");', 'origin = dxf_cross(file = "a.dxf", layer = "origin");',
]

# The same calls written so the lexer (src/lexer.l) still reads them as calls: a comment, a line break, a tab or a no-break space
# (lexer.l:244) between a name and its bracket or inside an argument, include straight after a number, code after a string that holds
# a comment's opening or an escaped quote, code after a comment that holds a quote, and a number too large for a double, which the
# lexer drops when it can't convert it (lexer.l:248-254).
WRITTEN_AROUND = [
    'linear_extrude/* a */("a.dxf");', 'rotate_extrude // a\n("a.dxf");', 'rotate_extrude\n\t("a.dxf");',
    'rotate_extrude\u00a0("a.dxf");', 'linear_extrude(file/**/= "a.dxf");',
    "x = 1.5include <a.scad>\ncube(1);",
    'echo("/*");\nimport("a.stl");\necho("*/");', 'echo("\\" /*");\nimport("a.stl"); // */', '/* " */ import("a.stl"); /* " */',
    'linear_extrude 1e999 ("a.dxf");', 'linear_extrude(1e999, "a.dxf");', 'rotate_extrude(file 1e999 = "a.dxf");',
]

# Code that reads nothing renders, including the names above inside strings and comments, which the lexer doesn't read as code.
ORDINARY = [
    "difference() {\n  cube([40, 20, 5.5]);\n  translate([5, 5, -1]) cylinder(h = 8, d = 4, $fn = 64);\n}",
    "linear_extrude(height = 5, center = true, twist = 30, $fn = 64) square(10);",
    "linear_extrude(5) circle(3, $fn = 64);",
    "linear_extrude(-2.5e1, center = true) square(4);",
    "rotate_extrude(angle = 270, convexity = 4, $fn = 64) translate([10, 0]) circle(2);",
    'linear_extrude(height = 1) text("Use <M3> screws to import the surface (top)");',
    '// import("a.stl") and include <a.scad> stay out.\n/* surface(file = "a.png") */\nsurface_depth = 2;\ncube(surface_depth);',
    "module my_import(size) cube(size);\nmy_import(3);",
    "if (len([1, 2]) == 2) linear_extrude(height = [3, 4][0] == 3 ? 3 : 4) square(2);",
]


class FileFormsTest(unittest.TestCase):
    def render(self, scad: str) -> OpenScad:
        programs = OpenScad()
        with mock.patch.object(modelmaker, "run_held", programs):
            modelmaker.render(scad)
        return programs

    def test_every_way_openscad_reads_a_file_is_refused_before_it_runs(self):
        for scad in READS_FILES + WRITTEN_AROUND:
            with self.subTest(scad=scad):
                programs = OpenScad()
                with mock.patch.object(modelmaker, "run_held", programs), self.assertRaises(ValueError) as caught:
                    modelmaker.render(scad)
                self.assertIn("code here can't read files", str(caught.exception))
                self.assertEqual(programs.commands, [], "refused before OpenSCAD runs")

    def test_code_that_reads_nothing_renders(self):
        for scad in ORDINARY:
            with self.subTest(scad=scad):
                self.assertTrue(self.render(scad).commands)

    def test_calls_nested_to_the_size_limit_are_read_in_one_pass(self):
        depth = (modelmaker.SCAD_MAX_CHARS - 1) // 25
        scad = "linear_extrude(height = " * depth + "1" + ")" * depth
        started = time.perf_counter()
        self.assertEqual(modelmaker.file_refusal(scad), "")
        self.assertLess(time.perf_counter() - started, 1.0, "reading each call's arguments again inside every outer call took seconds")


class HeldInTest(unittest.TestCase):
    """The limits every OpenSCAD run gets, read from what would be started, since prlimit and account switching are Linux's."""

    def start(self, *, account=None, slow=False):
        started: dict = {}
        killed: list = []

        class Process:
            pid = 4321
            returncode = 0

            def __init__(self, command, **options):
                started.update(command=command, options=options)
                self.args = command
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if slow and self.calls == 1:
                    raise subprocess.TimeoutExpired(self.args, timeout)
                return "", ""

        with mock.patch.object(modelmaker.subprocess, "Popen", Process), mock.patch.object(modelmaker, "_sandbox_account", lambda: account),                 mock.patch.object(modelmaker.os, "killpg", lambda pid, sig: killed.append((pid, sig)), create=True),                 mock.patch.object(modelmaker, "signal", types.SimpleNamespace(SIGKILL=9)):
            try:
                modelmaker.run_held(["openscad", "-o", "model.stl", "model.scad"], "/tmp/make", 150)
            except subprocess.TimeoutExpired:
                started["timed_out"] = True
        return started, killed

    def test_limits_a_bare_environment_and_its_own_process_group(self):
        started, _ = self.start()
        self.assertEqual(started["command"], ["prlimit", f"--as={4 << 30}", "--cpu=180", f"--fsize={200 << 20}", "--", "openscad", "-o", "model.stl", "model.scad"])
        options = started["options"]
        self.assertEqual(options["env"], {"HOME": "/tmp/make", "XDG_RUNTIME_DIR": "/tmp/make", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        self.assertEqual((options["cwd"], options["start_new_session"]), ("/tmp/make", True))
        self.assertNotIn("user", options)

    def test_as_root_it_runs_as_the_render_account(self):
        options = self.start(account=(990, 991))[0]["options"]
        self.assertEqual((options["user"], options["group"], options["extra_groups"]), (990, 991, []))

    def test_a_run_past_its_time_dies_with_its_whole_group(self):
        started, killed = self.start(slow=True)
        self.assertTrue(started.get("timed_out"))
        self.assertEqual(killed, [(4321, 9)])

    def test_the_render_account_or_nobody(self):
        accounts = {"nobody": types.SimpleNamespace(pw_uid=65534, pw_gid=65534)}

        def getpwnam(name):
            if name not in accounts:
                raise KeyError(name)
            return accounts[name]

        fake_pwd = types.SimpleNamespace(getpwnam=getpwnam)
        with mock.patch.dict(sys.modules, {"pwd": fake_pwd}), mock.patch.object(modelmaker.os, "geteuid", lambda: 0, create=True):
            self.assertEqual(modelmaker._sandbox_account(), (65534, 65534))
            accounts[modelmaker.ACCOUNT] = types.SimpleNamespace(pw_uid=990, pw_gid=991)
            self.assertEqual(modelmaker._sandbox_account(), (990, 991))
        with mock.patch.object(modelmaker.os, "geteuid", lambda: 1000, create=True):
            self.assertIsNone(modelmaker._sandbox_account(), "a bot not running as root can't switch accounts")


if __name__ == "__main__":
    unittest.main()
