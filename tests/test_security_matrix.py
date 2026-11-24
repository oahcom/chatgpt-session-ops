"""Structural integrity checks for the security acceptance matrix.

The matrix is a security evidence ledger. These tests ensure that its row
registry, status vocabulary, test citations, and code citations remain
machine-verifiable.

Run with:
    python -m pytest tests/test_security_matrix.py -q
"""

import ast
import re
import unittest
from pathlib import Path


class TestSecurityMatrix(unittest.TestCase):
    """守护 security-acceptance-matrix.md 的结构与证据引用。

    行号采用显式登记而非 1..N 连续整数，因为矩阵合法存在 4b。
    状态采用精确字符串集合，防止未知状态通过 PROVEN 前缀检查。
    test: 后缀若声明了测试模块，必须与真实定义文件一致。
    code: 的调用括号属于文档表示法，不属于 Python 符号名。
    """

    ROOT = Path(__file__).resolve().parents[1]
    MATRIX = ROOT / "references" / "security-acceptance-matrix.md"

    EXPECTED_ROW_IDS = (
        "1", "2", "3", "4", "4b", "5", "6", "7", "8", "9", "10",
        "11", "12", "13", "14", "15", "16", "17", "18", "19", "20",
        "21", "22",
    )

    ALLOWED_STATUSES = {
        "PROVEN",
        "PROVEN `mutation-checked`",
        "PROVEN `mutation-checked` for the lock; see note",
        "PROVEN, second layer unproven",
        "PROVEN, **partial**",
    }

    def _matrix_text(self):
        self.assertTrue(
            self.MATRIX.is_file(),
            "安全验收矩阵不存在: %s" % self.MATRIX.relative_to(self.ROOT),
        )
        return self.MATRIX.read_text(encoding="utf-8")

    def _rows(self):
        """解析主矩阵表格，保留 Markdown 状态字符串及原始行号。"""
        rows = []

        for line_number, line in enumerate(self._matrix_text().splitlines(), 1):
            if not line.lstrip().startswith("|"):
                continue

            cells = [
                cell.strip()
                for cell in line.strip().strip("|").split("|")
            ]

            # 主矩阵固定为五列：# / Scenario / Expected / Evidence / Status。
            if len(cells) != 5:
                continue

            row_id = cells[0]
            if not re.fullmatch(r"\d+[a-z]?", row_id):
                continue

            rows.append({
                "id": row_id,
                "line": line_number,
                "scenario": cells[1],
                "expected": cells[2],
                "evidence": cells[3],
                "status": cells[4],
            })

        return rows

    def _references(self):
        """提取主矩阵 Evidence 列中的 test: 和 code: 引用。

        test 引用允许：
            `test:test_example`
            `test:test_example` (test_module)

        code 引用允许：
            `code:NotAuthorized()`
            `code:some_method`
            `code:module.some_symbol`

        code 引用末尾的 () 是调用表示法，不纳入符号名。
        """
        references = []

        citation_pattern = re.compile(
            r"\b(test|code):"
            r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
            r"(?:\(\))?"
        )

        for row in self._rows():
            evidence = row["evidence"]

            for match in citation_pattern.finditer(evidence):
                kind, value = match.group(1), match.group(2)
                module_suffix = None

                if kind == "test":
                    suffix_match = re.match(
                        r"`\s*\((test_[A-Za-z0-9_]+)\)",
                        evidence[match.end():],
                    )
                    if suffix_match:
                        module_suffix = suffix_match.group(1)

                references.append({
                    "kind": kind,
                    "value": value,
                    "module": module_suffix,
                    "row": row["id"],
                    "line": row["line"],
                })

        return references

    def _test_definitions(self):
        """返回 {模块名: {test_function_name, ...}}。"""
        tests_root = self.ROOT / "tests"
        self.assertTrue(tests_root.is_dir(), "tests/ 目录不存在")

        definitions = {}

        for path in sorted(tests_root.glob("*.py")):
            try:
                tree = ast.parse(
                    path.read_text(encoding="utf-8"),
                    filename=str(path),
                )
            except (OSError, SyntaxError) as exc:
                self.fail("无法解析测试文件 %s: %s" % (path.name, exc))

            names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                )
                and node.name.startswith("test_")
            }

            definitions[path.stem] = names

        return definitions

    def _source_symbols(self):
        """收集 src/ 下 Python 文件中的类、函数及大写常量名称。"""
        source_root = self.ROOT / "src"
        self.assertTrue(source_root.is_dir(), "src/ 目录不存在")

        symbols = set()

        for path in sorted(source_root.rglob("*.py")):
            try:
                tree = ast.parse(
                    path.read_text(encoding="utf-8"),
                    filename=str(path),
                )
            except (OSError, SyntaxError) as exc:
                self.fail("无法解析源码文件 %s: %s" % (path, exc))

            for node in ast.walk(tree):
                if isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                ):
                    symbols.add(node.name)

                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Name)
                            and re.fullmatch(r"[A-Z][A-Z0-9_]*", target.id)
                        ):
                            symbols.add(target.id)

                elif isinstance(node, ast.AnnAssign):
                    target = node.target
                    if (
                        isinstance(target, ast.Name)
                        and re.fullmatch(r"[A-Z][A-Z0-9_]*", target.id)
                    ):
                        symbols.add(target.id)

        return symbols

    def test_matrix_row_registry_is_complete(self):
        """矩阵行号必须与登记集合完全一致，且不得重复。

        4b 是合法行号；不得把行号错误地限制为连续整数。
        新增、删除或重编号安全场景时，必须在同一变更中审查此登记。
        """
        rows = self._rows()
        actual_ids = [row["id"] for row in rows]

        self.assertTrue(rows, "没有解析到安全矩阵数据行，表格解析可能失效")
        self.assertEqual(
            len(actual_ids),
            len(set(actual_ids)),
            "安全矩阵存在重复行号: %s" % actual_ids,
        )
        self.assertEqual(
            tuple(actual_ids),
            self.EXPECTED_ROW_IDS,
            "安全矩阵行号集合或顺序发生变化；"
            "请审查新增/删除/重编号，并同步更新 EXPECTED_ROW_IDS。"
            "实际: %s" % (actual_ids,),
        )

    def test_status_values_are_exactly_allowed(self):
        """状态列必须精确匹配允许集合，不接受未知前缀或近似写法。"""
        rows = self._rows()

        for row in rows:
            with self.subTest(row=row["id"], line=row["line"]):
                self.assertIn(
                    row["status"],
                    self.ALLOWED_STATUSES,
                    "安全矩阵第 %s 行（文件第 %d 行）的状态不在允许集合中: %r"
                    % (row["id"], row["line"], row["status"]),
                )

    def test_every_row_has_evidence_reference(self):
        """每条安全场景必须有显式证据引用，不能退化成无证据意图。"""
        rows = self._rows()

        for row in rows:
            with self.subTest(row=row["id"], line=row["line"]):
                self.assertRegex(
                    row["evidence"],
                    r"\b(?:test|code|live|human):",
                    "安全矩阵第 %s 行（文件第 %d 行）没有任何证据引用"
                    % (row["id"], row["line"]),
                )

    def test_test_citations_resolve_and_file_suffix_matches(self):
        """test: 必须解析到真实测试定义；显式模块后缀必须准确。

        无后缀引用在 tests/*.py 中查找。
        有后缀引用只允许在指定模块文件中解析，不能跨文件碰巧命中。
        """
        definitions = self._test_definitions()
        references = [
            ref for ref in self._references()
            if ref["kind"] == "test"
        ]

        self.assertTrue(
            references,
            "没有解析到任何 test: 引用，引用解析规则可能失效",
        )

        for ref in references:
            name = ref["value"]
            module = ref["module"]

            with self.subTest(
                row=ref["row"],
                line=ref["line"],
                test=name,
                module=module,
            ):
                if module:
                    self.assertIn(
                        module,
                        definitions,
                        "矩阵第 %s 行的 test:%s 声明模块 %s，"
                        "但 tests/%s.py 不存在"
                        % (ref["row"], name, module, module),
                    )
                    self.assertIn(
                        name,
                        definitions[module],
                        "矩阵第 %s 行的 test:%s 声明位于 %s，"
                        "但该测试并未定义在 tests/%s.py"
                        % (ref["row"], name, module, module),
                    )
                else:
                    matches = [
                        module_name
                        for module_name, names in definitions.items()
                        if name in names
                    ]
                    self.assertTrue(
                        matches,
                        "矩阵第 %s 行引用了不存在的测试 %s"
                        % (ref["row"], name),
                    )

    def test_test_citation_module_suffixes_are_parsed(self):
        """每处显式 test: 模块后缀必须被识别并准确绑定。"""
        references = [
            ref for ref in self._references()
            if ref["kind"] == "test"
        ]

        actual = {
            ref["value"]: ref["module"]
            for ref in references
            if ref["module"] is not None
        }

        expected = {
            "test_identity_cannot_be_injected_through_arguments":
                "test_mcp_contract",
            "test_unexpected_typ_is_rejected_before_key_lookup":
                "test_mcp_alg_confusion",
            "test_missing_cryptography_is_classified_as_dependency_unavailable":
                "test_mcp_rs256",
            "test_refresh_can_retry_after_backoff_expires":
                "test_mcp_transport",
            "test_default_fetcher_pins_the_validated_address":
                "test_mcp_transport",
            "test_health_and_protected_resource_metadata_remain_public":
                "test_mcp_transport",
            "test_real_tools_call_rejects_missing_request_authentication":
                "test_mcp_sdk_integration",
        }

        self.assertEqual(
            actual,
            expected,
            "安全矩阵 test: 模块后缀解析不完整或映射错误。"
            "期望六处后缀均被识别；实际: %r" % actual,
        )

    def test_code_citations_resolve_to_source_symbols(self):
        """code: 必须解析到 src/ 中真实存在的 Python 符号。

        例如 code:NotAuthorized() 按符号 NotAuthorized 解析；
        调用括号不属于符号名，不需要别名表。
        """
        symbols = self._source_symbols()
        references = [
            ref for ref in self._references()
            if ref["kind"] == "code"
        ]

        self.assertTrue(
            references,
            "没有解析到任何 code: 引用，引用解析规则可能失效",
        )

        for ref in references:
            symbol = ref["value"].split(".")[-1]

            with self.subTest(
                row=ref["row"],
                line=ref["line"],
                symbol=symbol,
            ):
                self.assertIn(
                    symbol,
                    symbols,
                    "安全矩阵第 %s 行（文件第 %d 行）引用了不存在的代码符号: %s"
                    % (ref["row"], ref["line"], ref["value"]),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
