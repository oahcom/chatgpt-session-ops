# -*- coding: utf-8 -*-
"""SKILL.md references/ 索引与发布白名单的 B 侧一致性检查。

检查三个边界：
1. SKILL.md references/ 索引与仓库 references/*.md 双向一致。
2. RELEASE-AUTHORITY.md 白名单中的路径在 B 侧仓库存在。
3. 白名单引导句中的数量词与表格实际条目数一致。

不检查 A 侧实际文件集合。A 侧发布内容由发布阶段独立核验；
历史兼容文件不在白名单中，不代表它们在 A 侧不存在。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
REFERENCES = ROOT / "references"
POLICY = ROOT / "docs" / "RELEASE-AUTHORITY.md"


def _unique_heading(text, heading, description):
    """返回唯一标题的位置；缺失或重复时给出明确错误。"""
    matches = list(re.finditer(
        r"^" + re.escape(heading) + r"[ \t]*$",
        text,
        re.MULTILINE,
    ))
    if len(matches) != 1:
        raise AssertionError(
            "%s 必须恰好出现一次，实际出现 %d 次"
            % (description, len(matches))
        )
    return matches[0].start()


def _index_entries():
    """返回 references/ 索引条目：[(filename, line), ...]。"""
    text = SKILL.read_text(encoding="utf-8")

    start = _unique_heading(
        text, "### references/", "references/ 索引标题"
    )
    end = _unique_heading(
        text, "### tests/", "tests/ 索引标题"
    )

    if start >= end:
        raise AssertionError(
            "references/ 索引标题必须位于 tests/ 索引标题之前"
        )

    section = text[start:end]
    entries = []

    for line in section.splitlines():
        if not line.startswith("- `"):
            continue

        match = re.match(
            r"^- `([A-Za-z0-9._-]+\.md)`(?:[ \t]|$)",
            line,
        )
        if match is None:
            raise AssertionError(
                "无法解析 references/ 索引条目：%s" % line
            )

        entries.append((match.group(1), line))

    if not entries:
        raise AssertionError(
            "references/ 索引段没有解析到任何条目"
        )

    names = [name for name, _line in entries]
    if len(names) != len(set(names)):
        raise AssertionError(
            "references/ 索引存在重复文件名"
        )

    return entries


def _allowlist():
    """解析 RELEASE-AUTHORITY.md 中的三列白名单表格。"""
    text = POLICY.read_text(encoding="utf-8")

    entries = re.findall(
        r"^\| `([^`]+)` \| `[^`]+` \| [^|\r\n]+ \|[ \t]*$",
        text,
        re.MULTILINE,
    )

    if not entries:
        raise AssertionError(
            "RELEASE-AUTHORITY.md 三列白名单表格解析失败或为空"
        )

    if len(entries) != len(set(entries)):
        raise AssertionError(
            "RELEASE-AUTHORITY.md 白名单存在重复路径"
        )

    return set(entries)


class TestSkillReferenceIndex(unittest.TestCase):

    def test_every_index_entry_names_an_existing_file(self):
        entries = _index_entries()

        for name, _line in entries:
            with self.subTest(entry=name):
                self.assertTrue(
                    (REFERENCES / name).is_file(),
                    "SKILL.md 索引了 references/%s，但该文件不存在"
                    "（改名或删除后忘了改索引）" % name,
                )

    def test_no_reference_file_is_unindexed(self):
        indexed = {name for name, _line in _index_entries()}
        on_disk = {
            path.name for path in REFERENCES.glob("*.md")
        }

        self.assertEqual(
            set(),
            on_disk - indexed,
            "references/ 下有文件没进 SKILL.md 索引，读者无从发现它们",
        )

    def test_every_allowlisted_path_exists(self):
        for path in _allowlist():
            with self.subTest(path=path):
                self.assertTrue(
                    (ROOT / path).is_file(),
                    "白名单列了 %s，仓库里没有这个文件" % path,
                )

    def test_policy_count_word_matches_table(self):
        text = POLICY.read_text(encoding="utf-8")

        words = {
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
        }

        matches = list(re.finditer(
            r"^The following (\w+) files are the complete allowlist",
            text,
            re.MULTILINE,
        ))

        self.assertEqual(
            1,
            len(matches),
            "§2 的 allowlist 引导句必须恰好出现一次",
        )

        word = matches[0].group(1).lower()
        claimed = words.get(word)

        self.assertIsNotNone(
            claimed,
            "无法识别的数量词：%s" % word,
        )

        actual = len(_allowlist())

        self.assertEqual(
            claimed,
            actual,
            "§2 声称 %s 个文件，表格实际 %d 行"
            % (word, actual),
        )


if __name__ == "__main__":
    unittest.main()
