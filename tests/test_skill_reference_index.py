# -*- coding: utf-8 -*-
"""SKILL.md 的 references/ 索引与发布白名单必须各自自洽。

三起真实漂移，同一类根因——「同一事实写了两份」：
  - #39 新增 `references/mcp-tools.md` 与 `mcp-oauth-connector-contract.md`，
    文件进了仓库、`SKILL.md` 正文里也引用了，但 `### references/` 索引里没有。
    读者按索引找文档，这两份等于不存在。
  - #21 建立白名单时表格 7 行、正文两处写 `six`；#39 把表格加到 8 行、
    正文只补了 6 处里的 4 处。按步骤 6 字面执行的运维会少发布两个文件。
  - `security-acceptance-matrix.md` 被索引标为必读，却不在白名单，
    A 侧没有这个文件——索引指向一个打不开的路径。

本文件只断言 B 侧可自证的部分：索引与 references/ 双向齐全、白名单表格
与正文数量词一致。**A 侧实际有哪些文件 B 侧看不到**，因此「索引里的文件
在 A 上是否存在」不在这里断言——那由发布时的 A 侧核验负责（见
`docs/RELEASE-AUTHORITY.md` §6）。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
REFERENCES = ROOT / "references"
POLICY = ROOT / "docs" / "RELEASE-AUTHORITY.md"


def _index_entries():
    """`### references/` 段内的索引条目：[(filename, line), ...]。"""
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("### references/")
    end = text.index("### tests/")
    entries = []
    for line in text[start:end].splitlines():
        m = re.match(r"^- `([A-Za-z0-9._\-]+\.md)`", line)
        if m:
            entries.append((m.group(1), line))
    return entries


def _allowlist():
    text = POLICY.read_text(encoding="utf-8")
    return set(re.findall(r"^\| `([^`]+)` \| `[^`]+` \|", text, re.M))


class TestSkillReferenceIndex(unittest.TestCase):

    def test_every_index_entry_names_an_existing_file(self):
        entries = _index_entries()
        self.assertGreaterEqual(len(entries), 8, "索引解析失败，规则可能没匹配上")
        for name, _line in entries:
            with self.subTest(entry=name):
                self.assertTrue(
                    (REFERENCES / name).is_file(),
                    "SKILL.md 索引了 references/%s，但该文件不存在"
                    "（改名或删除后忘了改索引）" % name
                )

    def test_no_reference_file_is_unindexed(self):
        indexed = {n for n, _ in _index_entries()}
        on_disk = {p.name for p in REFERENCES.glob("*.md")}
        self.assertEqual(
            set(), on_disk - indexed,
            "references/ 下有文件没进 SKILL.md 索引，读者无从发现它们"
        )

    def test_every_allowlisted_path_exists(self):
        """白名单里的条目必须真实存在，否则发布步骤会卡在「确认存在」却说不清。"""
        for path in _allowlist():
            with self.subTest(path=path):
                self.assertTrue(
                    (ROOT / path).is_file(),
                    "白名单列了 %s，仓库里没有这个文件" % path
                )

    def test_policy_count_word_matches_table(self):
        """§2「以下 N 个文件」的 N 必须等于表格行数——#21/#39 就是这里漂的。"""
        text = POLICY.read_text(encoding="utf-8")
        words = {"six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
        m = re.search(r"The following (\w+) files are the complete allowlist", text)
        self.assertIsNotNone(m, "§2 的 allowlist 引导句解析失败")
        claimed = words.get(m.group(1).lower())
        self.assertIsNotNone(claimed, "无法识别的数量词：%s" % m.group(1))
        self.assertEqual(
            claimed, len(_allowlist()),
            "§2 声称 %s 个文件，表格实际 %d 行" % (m.group(1), len(_allowlist()))
        )


if __name__ == "__main__":
    unittest.main()
