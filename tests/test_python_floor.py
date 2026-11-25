"""The declared Python floor, and the CI job that has to exercise it (#93).

Three places claimed 3.8+: README.md, authority.py and mcp_server.py. The
send path uses `datetime.fromisoformat`, whose compact (`20261125T182548+0000`)
and no-seconds (`2026-11-25T18:25`) forms only arrived in 3.11, and
`check_oneshot`'s docstring promises both. On 3.8 that call raises
`ScheduleError("scheduledAt 不是 ISO 8601")` for a string that is ISO 8601 —
a wrong diagnosis, not merely a refused input.

The claim was also unverifiable: every CI job pinned 3.11, so a 3.11-only
matrix cannot tell "3.8 compatible" from "3.11 compatible".

Both halves are asserted here. The floor is one number, declared in four
places, and the interpreter running this file has to be at or above it —
which makes the claim fail on an interpreter that cannot honour it, instead
of failing silently in production on the send path.
"""

import datetime
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DECLARATIONS = (
    ("README.md", r"需要 (\d+\.\d+)\+"),
    ("src/session_ops/authority.py", r"Python (\d+\.\d+)\+"),
    ("src/session_ops/mcp_server.py", r"Python source syntax: (\d+\.\d+)\+"),
    (".github/workflows/ci.yml", r'python-version: "(\d+\.\d+)"'),
)

# The forms check_oneshot's docstring promises: 空格分隔, 紧凑, Z, 无秒.
DOCUMENTED_FORMS = (
    "2026-11-25T18:25:48+00:00",
    "20261125T182548+0000",
    "20261125T182548Z",
    "2026-11-25 18:25:48+00:00",
    "2026-11-25T18:25+00:00",
)


def declared_minors() -> dict:
    found = {}
    for relative, pattern in DECLARATIONS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        matches = re.findall(pattern, text)
        if not matches:
            raise AssertionError("%s declares no Python floor" % relative)
        found[relative] = set(matches)
    return found


class PythonFloorTests(unittest.TestCase):
    def test_every_declaration_names_one_floor(self):
        minors = set()
        for relative, values in declared_minors().items():
            with self.subTest(file=relative):
                self.assertEqual(
                    len(values), 1, "%s names %r" % (relative, sorted(values))
                )
                minors |= values

        self.assertEqual(
            len(minors), 1,
            "the declared floor disagrees: %r" % (declared_minors(),),
        )

    def test_ci_runs_the_declared_floor(self):
        # A floor that CI never runs is an unverified claim, which is how 3.8
        # survived in the docs while the code needed 3.11.
        minors = declared_minors()
        floor = next(iter(minors["README.md"]))
        ci = minors[".github/workflows/ci.yml"]
        self.assertIn(floor, ci, "CI does not run the declared floor: %r" % (ci,))

    def test_the_floor_supports_the_documented_isoformats(self):
        minors = declared_minors()
        floor = tuple(int(part) for part in next(iter(minors["README.md"])).split("."))
        self.assertGreaterEqual(
            sys.version_info[:2], floor,
            "this interpreter is older than the declared floor %r" % (floor,),
        )

        for form in DOCUMENTED_FORMS:
            with self.subTest(form=form):
                parsed = datetime.datetime.fromisoformat(form.replace("Z", "+00:00"))
                self.assertIsNotNone(parsed.tzinfo)


if __name__ == "__main__":
    unittest.main()
