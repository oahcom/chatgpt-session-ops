# -*- coding: utf-8 -*-
"""F01：文档 evidence 声明完整性（独立契约，不与被审文档同源）。

原有 test_hardening.py 里的 test_claimed_regression_count_matches_actual 只校验
「已匹配到的声明数字对不对」，抓不到「声明整条消失」。本文件用 tests/ 下的独立
契约文件充当预期数量的唯一来源，补上这一侧。
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class TestEvidenceClaimDeclarationCount(unittest.TestCase):

    def _read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_regression_claim_declaration_count_matches_contract(self):
        """Every documented regression-count claim must be explicitly accounted for."""
        contract_path = os.path.join(
            HERE, "evidence_claim_contract.json"
        )

        self.assertTrue(
            os.path.isfile(contract_path),
            "F01 契约文件缺失：tests/evidence_claim_contract.json"
        )

        with open(contract_path, "r", encoding="utf-8") as f:
            contract = __import__("json").load(f)

        self.assertIsInstance(
            contract, dict,
            "F01 契约格式错误：根对象必须是 JSON object"
        )
        self.assertIs(
            type(contract.get("schema_version")), int,
            "F01 契约错误：schema_version 必须是整数"
        )
        self.assertEqual(
            contract.get("schema_version"), 1,
            "F01 契约版本错误：schema_version 必须为 1"
        )
        self.assertEqual(
            contract.get("scope"),
            "test_hardening.py regression-count declarations only",
            "F01 契约范围错误：不得擅自改变计数域"
        )

        expected = contract.get("expected_declaration_count")
        self.assertIs(
            type(expected), int,
            "F01 契约错误：expected_declaration_count 必须是整数"
        )
        self.assertGreater(
            expected, 0,
            "F01 契约错误：expected_declaration_count 必须大于 0"
        )

        claims = []
        files = (
            ("SKILL.md", ("SKILL.md",)),
            ("baselines.md", ("references", "baselines.md")),
            ("evidence-matrix.md",
             ("references", "evidence-matrix.md")),
        )
        pats = (
            r"当前回归 (\d+) 条",
            r"当前 (\d+) 条回归",
            r"全绿（当前 (\d+) 条）",
            r"离线回归（(\d+) 条）",
            r"(\d+) 条，覆盖 id 校验",
            r"会直接红。当前 (\d+) 条。",
        )

        for label, parts in files:
            text = self._read(*parts)
            for pat in pats:
                for n in re.findall(pat, text):
                    claims.append((label, int(n)))

        self.assertEqual(
            len(claims),
            expected,
            (
                "evidence 声明数量不符："
                "契约要求 %d 条，实际匹配到 %d 条；"
                "可能存在声明删除、遗漏或新增，"
                "必须经过审查后才能更新契约。"
            ) % (expected, len(claims))
        )
