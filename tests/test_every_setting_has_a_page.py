"""Every stored setting can be seen and changed on the dashboard, whichever command also sets it."""
import re
import unittest
from pathlib import Path

from captainguild.bot import CORE_SCHEMA
from captainguild.core.perms import SETTINGS_SCHEMA as COMMANDS_SCHEMA
from captainguild.core.validate import Schema
from captainguild.modules import load_all
from captainguild.modules.greetings import Greetings

PAGES = Path(__file__).resolve().parent.parent / "captainguild" / "web" / "static" / "js"


def leaves(prefix: str, schema: Schema):
    for name, spec in schema.fields.items():
        if isinstance(spec, Schema):
            yield from leaves(f"{prefix}.{name}", spec)
        else:
            yield f"{prefix}.{name}", name


class EverySettingTest(unittest.TestCase):
    def test_every_setting_is_named_on_a_page(self):
        text = "\n".join(path.read_text(encoding="utf-8") for path in PAGES.rglob("*.js"))
        schemas = [("core", CORE_SCHEMA), ("commands", COMMANDS_SCHEMA), *((module.key, module.schema) for module in load_all() if module.schema is not None)]
        missing = [path for key, schema in schemas for path, name in leaves(key, schema) if not re.search(rf"\b{re.escape(name)}\b", text)]
        self.assertEqual(missing, [])

    def test_the_birthday_reset_goes_to_the_default_hour(self):
        page = (PAGES / "pages" / "greetings.js").read_text(encoding="utf-8")
        self.assertEqual(int(re.search(r"form\.set\('birthday_hour', (\d+)\)", page).group(1)), Greetings.schema.fields["birthday_hour"].default)
