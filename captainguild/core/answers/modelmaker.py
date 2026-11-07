"""Printable models for /make_model.

The model writes the part in OpenSCAD and calls render_model, which runs OpenSCAD on this machine to make a 3MF file and a preview
picture, and reports the finished size back so the model can fix mistakes before it replies. The member gets the 3MF, the editable
.scad source and the preview.

OpenSCAD runs on code a model wrote from what a member typed, so each render is held in: a fresh temporary folder, an account with no
login when the bot runs as root, limits on memory, CPU time and file size through prlimit, a time limit, a process group that dies
whole, and none of OpenSCAD's ways to read a file (include and use, import and its older forms, surface, the DXF functions, and the
file the extrusions can take), so the code can't read files from the machine. It needs the openscad, xvfb-run and prlimit programs, and
without them /make_model says it's unavailable.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import signal
import subprocess
import tempfile
import zipfile
from typing import Any

from .engine import Answer

MAX_ROUNDS = 8
MAX_TOKENS = 20000  # the OpenSCAD source and the thinking before it
MAX_RENDERS = 4  # renders one answer runs: a first try and three fixes, each up to six and a half minutes
# Renders run as this account when the bot runs as root, and as nobody when it doesn't exist.
ACCOUNT = "makemodel"

RENDER_SECONDS = 150  # the full render to a mesh, and again for the 3MF
PREVIEW_SECONDS = 90
RENDER_MEMORY = 4 << 30  # bytes of address space
RENDER_CPU = 180  # CPU seconds
RENDER_FILE_SIZE = 200 << 20  # the largest file the renderer may write
SCAD_MAX_CHARS = 60_000
MODEL_FILE_MAX_BYTES = 8_000_000  # leaves room under Discord's 10 MB upload limit
PROGRAMS = ("openscad", "xvfb-run", "prlimit")

# Every way OpenSCAD 2021.01 reads a file or a path, from its source: include <x> and use <x>, which its lexer reads wherever it meets
# them (src/lexer.l:142 and :151), the modules import, import_stl, import_off and import_dxf (src/import.cc:247-251), surface
# (src/surface.cc:329), dxf_linear_extrude and dxf_rotate_extrude (src/linearextrude.cc:185, src/rotateextrude.cc:133), the functions
# dxf_dim and dxf_cross (src/dxfdim.cc:249-254), and linear_extrude and rotate_extrude given a file, which is their first parameter, so
# an unnamed argument fills it too (src/linearextrude.cc:59 and :80, src/rotateextrude.cc:55 and :71). linear_extrude reads only a
# string there and takes a number as the height, while rotate_extrude reads whatever it's given.
_READERS = frozenset({"import", "import_stl", "import_off", "import_dxf", "surface", "dxf_linear_extrude", "dxf_rotate_extrude", "dxf_dim",
                      "dxf_cross"})
_EXTRUDES = {
    "linear_extrude": "linear_extrude reads a file given as file = or as an unnamed argument that isn't a number, and code here can't read "
    "files. Name its arguments, such as linear_extrude(height = 5).",
    "rotate_extrude": "rotate_extrude reads a file given as file = or as an unnamed argument, and code here can't read files. Name its "
    "arguments, such as rotate_extrude(angle = 360).",
}
# The code is read the way OpenSCAD's lexer reads it (src/lexer.l), so a comment, a line break or a no-break space between a name and its
# bracket doesn't hide a call, and a name inside a string or a comment isn't one. These are its rules for the tokens that matter here.
_LIBRARY = re.compile(r"(?:include|use)[ \t\r\n]*<", re.I)
_NAME = re.compile(r"\$?[A-Za-z0-9_]+")
_NUMBERS = tuple(re.compile(pattern) for pattern in (r"[0-9]+(?:[Ee][+-]?[0-9]+)?", r"[0-9]*\.[0-9]+(?:[Ee][+-]?[0-9]+)?",
                                                     r"[0-9]+\.[0-9]*(?:[Ee][+-]?[0-9]+)?"))
_PAIRS = ("<=", ">=", "==", "!=", "&&", "||")
_OPENING = (("mark", "("), ("mark", "["), ("mark", "{"))
_CLOSING = (("mark", ")"), ("mark", "]"), ("mark", "}"))
_SPACE = " \t\r\n\u00a0"  # the lexer skips a no-break space as it does a space (src/lexer.l:244)
_VERTEX = re.compile(rb'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"')


def available() -> bool:
    return all(shutil.which(program) for program in PROGRAMS)


def _sandbox_account() -> tuple[int, int] | None:
    """The user and group IDs renders run as, or None when this process can't switch accounts."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    import pwd

    try:
        entry = pwd.getpwnam(ACCOUNT)
    except KeyError:
        entry = pwd.getpwnam("nobody")
    return entry.pw_uid, entry.pw_gid


def run_held(command: list[str], folder: str, seconds: int) -> subprocess.CompletedProcess:
    """Run a command held in by the limits above. On a timeout the whole process group dies, so the X server xvfb-run starts for a
    preview can't outlive it."""
    limits = ["prlimit", f"--as={RENDER_MEMORY}", f"--cpu={RENDER_CPU}", f"--fsize={RENDER_FILE_SIZE}", "--"]
    env = {"HOME": folder, "XDG_RUNTIME_DIR": folder, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    options: dict[str, Any] = {}
    account = _sandbox_account()
    if account is not None:
        options = {"user": account[0], "group": account[1], "extra_groups": []}
    process = subprocess.Popen(limits + command, cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               start_new_session=True, **options)
    try:
        stdout, stderr = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def mesh_stats(model_3mf: bytes) -> tuple[tuple[float, ...], tuple[float, ...], int]:
    """The corners of a 3MF file's bounding box, and its triangle count."""
    with zipfile.ZipFile(io.BytesIO(model_3mf)) as archive:
        xml = archive.read("3D/3dmodel.model")
    lows, highs = [float("inf")] * 3, [float("-inf")] * 3
    for match in _VERTEX.finditer(xml):
        for axis in range(3):
            value = float(match.group(axis + 1))
            lows[axis] = min(lows[axis], value)
            highs[axis] = max(highs[axis], value)
    if lows[0] == float("inf"):
        raise ValueError("The rendered model is empty. Check that the code produces a solid.")
    return tuple(lows), tuple(highs), xml.count(b"<triangle ")


def _tokens(scad: str):
    """The code's tokens as OpenSCAD's lexer reads them, as (kind, text), leaving out spaces and comments. It stops at include <x> or
    use <x>, which the lexer reads as it meets them."""
    at, end = 0, len(scad)
    while at < end:
        char = scad[at]
        if char in _SPACE:
            at += 1
        elif scad.startswith("//", at):  # to the end of the line, which a carriage return alone doesn't end
            newline = scad.find("\n", at)
            at = end if newline < 0 else newline + 1
        elif scad.startswith("/*", at):
            close = scad.find("*/", at + 2)
            at = end if close < 0 else close + 2
        elif char == '"':  # a backslash escapes the next character, a quote among them, and a string may run over lines
            start, at = at, at + 1
            while at < end and scad[at] != '"':
                at += 2 if scad[at] == "\\" else 1
            at += 1
            yield "string", scad[start:at]
        elif library := _LIBRARY.match(scad, at):
            yield "library", library.group()
            return
        else:
            # The longest match wins, and a number wins a tie, so 1e5 is a number, 1e5x a name, and 1.5import a number and a name.
            name = _NAME.match(scad, at)
            number = max((len(match.group()) for pattern in _NUMBERS if (match := pattern.match(scad, at))), default=0)
            if name and len(name.group()) > number:
                yield "name", name.group()
                at = name.end()
            elif number:
                yield "number", scad[at:at + number]
                at += number
            else:
                width = 2 if scad[at:at + 2] in _PAIRS else 1
                yield "mark", scad[at:at + width]
                at += width


def _arguments(tokens: list[tuple[str, str]], opening: int, closing: dict[int, int]) -> list[list[tuple[str, str]]]:
    """The arguments of the call whose bracket is at tokens[opening], each as its tokens, without empty ones. A bracket inside an
    argument stands for everything up to its closing bracket, so each token is read once however deep the calls nest."""
    arguments: list[list[tuple[str, str]]] = [[]]
    at, end = opening + 1, closing.get(opening, len(tokens))
    while at < end:
        if tokens[at] == ("mark", ","):
            arguments.append([])
        else:
            arguments[-1].append(tokens[at])
            if tokens[at] in _OPENING:
                at = closing.get(at, end)
        at += 1
    return [argument for argument in arguments if argument]


def file_refusal(scad: str) -> str:
    """Words for the model when the code would read a file, and "" when it wouldn't."""
    tokens = list(_tokens(scad))
    closing: dict[int, int] = {}  # where each bracket closes
    opened: list[int] = []
    for at, token in enumerate(tokens):
        if token in _OPENING:
            opened.append(at)
        elif token in _CLOSING and opened:
            closing[opened.pop()] = at
    for at, (kind, text) in enumerate(tokens):
        if kind == "library":
            return "include and use read other files, and code here can't read files. Build the part from primitives."
        if kind != "name":
            continue
        word = text.lower()
        if word in _READERS:
            return f"{text} reads a file, and code here can't read files. Build the part from primitives."
        if word not in _EXTRUDES:
            continue
        opening = at + 1
        while opening < len(tokens) and tokens[opening][0] == "number":  # a number too large for a double, which the lexer drops
            opening += 1
        if opening == len(tokens) or tokens[opening] != ("mark", "("):
            continue
        for argument in _arguments(tokens, opening, closing):
            named = len(argument) > 1 and argument[0][0] == "name" and argument[1] == ("mark", "=")
            if named and argument[0][1].lower() == "file":
                return _EXTRUDES[word]
            plain_number = [kind for kind, _ in argument] == ["number"] or (
                len(argument) == 2 and argument[0] in (("mark", "-"), ("mark", "+")) and argument[1][0] == "number")
            if not named and (word == "rotate_extrude" or not plain_number):
                return _EXTRUDES[word]
    return ""


def render(scad: str) -> dict:
    """OpenSCAD source as a 3MF file and a preview PNG. Raises ValueError with words for the model when the code isn't allowed or
    doesn't render."""
    if len(scad) > SCAD_MAX_CHARS:
        raise ValueError(f"The code is over {SCAD_MAX_CHARS} characters. Simplify it.")
    refusal = file_refusal(scad)
    if refusal:
        raise ValueError(refusal)
    folder = tempfile.mkdtemp(prefix="make_model-")
    try:
        with open(os.path.join(folder, "model.scad"), "w", encoding="utf-8") as handle:
            handle.write(scad)
        account = _sandbox_account()
        if account is not None:
            for path in (folder, os.path.join(folder, "model.scad")):
                os.chown(path, *account)
        # The member's code is evaluated once, into a mesh, and the 3MF and the preview are both made from that mesh, so the picture
        # shows what's in the file. OpenSCAD's quick preview draws the CSG instead, and it showed a solid brace as a hole.
        try:
            result = run_held(["openscad", "-o", "model.stl", "model.scad"], folder, RENDER_SECONDS)
        except subprocess.TimeoutExpired:
            raise ValueError(f"The render took over {RENDER_SECONDS} seconds. Lower $fn or avoid minkowski on detailed shapes.") from None
        lines = (result.stdout + result.stderr).splitlines()
        mesh_path = os.path.join(folder, "model.stl")
        if result.returncode != 0 or not os.path.exists(mesh_path) or os.path.getsize(mesh_path) == 0:
            errors = [line for line in lines if "ERROR" in line] or lines
            raise ValueError("OpenSCAD failed:\n" + "\n".join(errors[-12:]))
        with open(os.path.join(folder, "mesh.scad"), "w", encoding="utf-8") as handle:
            handle.write('import("model.stl");\n')
        if account is not None:
            os.chown(os.path.join(folder, "mesh.scad"), *account)
        try:
            packed = run_held(["openscad", "-o", "model.3mf", "mesh.scad"], folder, RENDER_SECONDS)
        except subprocess.TimeoutExpired:
            raise ValueError("Packing the model took too long. Lower $fn or simplify the shape.") from None
        model_path = os.path.join(folder, "model.3mf")
        if packed.returncode != 0 or not os.path.exists(model_path):
            raise ValueError("OpenSCAD couldn't write the 3MF file:\n" + "\n".join((packed.stdout + packed.stderr).splitlines()[-6:]))
        with open(model_path, "rb") as handle:
            model = handle.read()
        if len(model) > MODEL_FILE_MAX_BYTES:
            raise ValueError("The model file is too large to post. Lower $fn or simplify the shape.")
        lows, highs, triangles = mesh_stats(model)
        warnings = [line.strip() for line in lines if "WARNING" in line][:6]

        preview = None
        try:
            shot = run_held(["xvfb-run", "-a", "-s", "-screen 0 1280x1024x24", "openscad", "-o", "preview.png", "--render", "--imgsize=1200,900",
                             "--viewall", "--autocenter", "--colorscheme=Tomorrow", "mesh.scad"], folder, PREVIEW_SECONDS)
            preview_path = os.path.join(folder, "preview.png")
            if shot.returncode == 0 and os.path.exists(preview_path):
                with open(preview_path, "rb") as handle:
                    preview = handle.read()
        except subprocess.TimeoutExpired:
            pass  # the model still goes out without a preview
        return {"model": model, "preview": preview, "lows": lows, "highs": highs, "triangles": triangles, "warnings": warnings}
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def file_name(name: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:40]
    return slug or "model"


async def render_model(args: dict, answer: Answer) -> tuple[str, bool]:
    scad = str(args.get("scad", ""))
    try:
        out = await asyncio.to_thread(render, scad)
    except ValueError as error:
        return str(error), True
    except FileNotFoundError:
        return "Model rendering isn't available on this bot. Tell the member it's unavailable right now.", True
    lows, highs = out["lows"], out["highs"]
    size = f"{highs[0] - lows[0]:.1f} x {highs[1] - lows[1]:.1f} x {highs[2] - lows[2]:.1f} mm"
    name = file_name(args.get("name", "model"))
    answer.files = [(f"{name}.3mf", out["model"]), (f"{name}.scad", scad.encode("utf-8"))]
    answer.image = out["preview"]
    answer.image_name = "preview.png"
    answer.image_label = "Model"
    answer.image_caption = f"{name}.3mf, {size}"
    # The whole source goes to the word screen: embossed text, and also variable names, since one test model carried a blocked word in
    # a variable's name and none in its strings.
    answer.screen_text = [scad]
    report = f"Rendered. Size {size} (X by Y by Z). Lowest point z = {lows[2]:.2f} mm. {out['triangles']} triangles."
    if out["warnings"]:
        report += " OpenSCAD warnings: " + " | ".join(out["warnings"])
    return report, False
