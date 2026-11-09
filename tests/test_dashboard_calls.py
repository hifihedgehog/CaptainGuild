"""Every dashboard page calls only the request functions the API client has. A misspelled one throws in the browser when clicked, and nothing
else catches it."""
import re
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "captainguild" / "web" / "static" / "js"
IMPORT = re.compile(r"import \{([^}]*)\} from '[./]*api\.js'")
CALL = re.compile(r"\b(api|raw)\.(\w+)\(")


def defined(name: str) -> set[str]:
    """The functions an object exported from api.js holds."""
    text = (SCRIPTS / "api.js").read_text(encoding="utf-8")
    body = re.search(rf"export const {name} = \{{(.*?)\n\}};", text, re.S)
    return set(re.findall(r"^\s*(\w+):", body.group(1), re.M))


class CallsTest(unittest.TestCase):
    def test_every_page_calls_functions_the_client_has(self):
        known = {"api": defined("api"), "raw": defined("raw")}
        self.assertIn("del", known["api"])
        wrong = []
        for path in SCRIPTS.rglob("*.js"):
            text = path.read_text(encoding="utf-8")
            imported = {name.strip() for found in IMPORT.findall(text) for name in found.split(",")}
            for client, method in CALL.findall(text):
                if client in imported and method not in known[client]:
                    wrong.append(f"{path.relative_to(SCRIPTS)}: {client}.{method}")
        self.assertEqual(wrong, [])


if __name__ == "__main__":
    unittest.main()
