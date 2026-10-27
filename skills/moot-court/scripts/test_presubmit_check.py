#!/usr/bin/env python3
"""presubmit_check.py 回归：真实阻断、误伤边界、输入错误与报告不泄露。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("presubmit_check.py")
ASSIGNMENT = {
    "schema_version": 1, "run_id": "synthetic-run", "turn_id": "T-0002",
    "role": "plaintiff", "material_version": 1, "read_through": 1, "responds_to": [],
}

DOCUMENTS = [
    {
        "id": "D-001",
        "title": "合同摘录",
        "locator": "合同第3.1条",
        "text": "开发费总额30万元，验收以双方签署《项目验收确认单》为准。",
        "visible_to": ["all"],
    },
    {
        "id": "D-002",
        "title": "确认邮件",
        "locator": "邮件",
        "text": "甲公司回复：我方已接收测试版本并投入生产环境使用三个月。",
        "visible_to": ["all"],
    },
    {
        "id": "P-001",
        "title": "乙公司内部UAT测试笔记",
        "locator": "内部笔记",
        "text": "内部UAT清单42项通过，3项为非致命界面问题；未取得甲公司签署。",
        "visible_to": ["plaintiff"],
    },
    {
        "id": "K-001",
        "title": "运维台账",
        "locator": "台账",
        "text": "尾款结算表显示已扣减8万元质保金，实际应付22万元。",
        "visible_to": ["defendant"],
    },
    {
        "id": "S-001",
        "title": "双方庭前程序约定",
        "locator": "程序",
        "text": "双方同意就验收争议先行调解，调解记录不入卷。",
        "visible_to": ["plaintiff", "defendant"],
    },
]


def run_check(body, *, documents=None, citations=None, submission=None, manifest=None,
              packet=None):
    documents = DOCUMENTS if documents is None else documents
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        packet_path = tmp / "packet.json"
        manifest_path = tmp / "manifest.json"
        submission_path = tmp / "submission.json"
        packet_path.write_text(json.dumps(packet if packet is not None else {
            "assignment": ASSIGNMENT,
            "documents": [doc for doc in documents
                          if doc["visible_to"] == ["all"] or "plaintiff" in doc["visible_to"]],
        }, ensure_ascii=False), encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest if manifest is not None else {"documents": documents},
                                            ensure_ascii=False), encoding="utf-8")
        submission_path.write_text(json.dumps(submission if submission is not None else {
            **ASSIGNMENT,
            "body": body,
            "citations": citations if citations is not None else ["D-001"],
        }, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--packet", str(packet_path),
             "--manifest", str(manifest_path), "--submission", str(submission_path),
             "--json"],
            capture_output=True, text=True)
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc, payload


class PresubmitCheckTest(unittest.TestCase):
    def test_clean_submission_passes(self):
        proc, out = run_check("原告请求给付30万元，依据 D-001 的约定。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(out["ok"])
        self.assertEqual(out["findings"], [])
        self.assertEqual(out["private_docs_considered"], 3)

    def test_own_private_id_and_text_block(self):
        proc, out = run_check("原告内部材料 P-001 显示：内部UAT清单42项通过，3项为非致命界面问题。")
        self.assertEqual(proc.returncode, 1)
        rules = [f["rule"] for f in out["findings"]]
        self.assertIn("MC-PRIVATE-ID", rules)
        self.assertIn("MC-PRIVATE-TEXT", rules)

    def test_opponent_private_leak_detected_from_manifest(self):
        proc, out = run_check("原告补充：运维台账显示已扣减8万元质保金，实际应付22万元。")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("MC-PRIVATE-TEXT", [f["rule"] for f in out["findings"]])

    def test_private_citation_blocks(self):
        proc, out = run_check("原告请求给付。", citations=["K-001"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("MC-PRIVATE-CITATION", [f["rule"] for f in out["findings"]])

    def test_multiparty_material_still_private_for_open_record(self):
        proc, out = run_check("原告同意就验收争议先行调解，调解记录不入卷。")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("MC-PRIVATE-TEXT", [f["rule"] for f in out["findings"]])

    def test_quoting_public_text_with_newline_is_not_blocked(self):
        documents = json.loads(json.dumps(DOCUMENTS, ensure_ascii=False))
        documents[1]["text"] += "\n"
        documents[2]["text"] = "庭前备忘：" + documents[1]["text"].strip() + "据此主张验收合格。"
        proc, out = run_check("我方已接收测试版本并投入生产环境使用三个月（D-002 原文）。",
                              documents=documents)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(out["ok"])

    def test_private_memo_quoting_public_text_does_not_false_block(self):
        documents = json.loads(json.dumps(DOCUMENTS, ensure_ascii=False))
        documents[2]["text"] = "庭前要点：" + documents[1]["text"].strip() + "另拟申请证人出庭。"
        proc, out = run_check("原告指出：我方已接收测试版本并投入生产环境使用三个月。",
                              documents=documents)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(out["ok"])

    def test_generic_private_title_does_not_false_block(self):
        documents = json.loads(json.dumps(DOCUMENTS, ensure_ascii=False))
        documents[2]["title"] = "合同"
        proc, out = run_check("原告依据合同主张价款。", documents=documents)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("MC-PRIVATE-ID", [f["rule"] for f in out["findings"]])

    def test_report_does_not_echo_private_content(self):
        body = "原告内部材料 P-001 显示：内部UAT清单42项通过，3项为非致命界面问题。"
        proc, out = run_check(body)
        self.assertEqual(proc.returncode, 1)
        text = proc.stdout
        for secret in ("P-001", "内部UAT清单42项通过", "非致命界面问题", "运维台账",
                       "8万元质保金"):
            self.assertNotIn(secret, text)
        self.assertIn("private_doc_", text)

    def test_stale_packet_is_rejected(self):
        submission = {
            **ASSIGNMENT,
            "body": "原告请求。", "citations": ["D-001"],
            "turn_id": "T-0009", "role": "defendant", "material_version": 2,
        }
        proc, _ = run_check("", submission=submission)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("turn_id", proc.stderr)

    def test_stale_manifest_and_missing_identity_rejected(self):
        manifest = json.loads(json.dumps({"documents": DOCUMENTS}, ensure_ascii=False))
        manifest["documents"][0]["text"] += "新材料版本。"
        proc, _ = run_check("原告请求。", manifest=manifest)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("快照", proc.stderr)
        proc, _ = run_check("", submission={"body": "原告请求。", "citations": []})
        self.assertEqual(proc.returncode, 2)
        self.assertIn("派发字段", proc.stderr)

    def test_bad_input_types_exit_2_without_traceback(self):
        cases = [
            ({"documents": {"id": "D-001"}}, "manifest"),
            ({"documents": ["text"]}, "manifest"),
            ({"documents": [{"id": 42, "title": "t", "text": "x",
                             "visible_to": ["all"]}]}, "manifest"),
        ]
        for manifest, _ in cases:
            with self.subTest(manifest=manifest):
                proc, _ = run_check("原告请求。", manifest=manifest)
                self.assertEqual(proc.returncode, 2)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertIn("manifest", proc.stderr)
        proc, _ = run_check("", submission=["not", "an", "object"])
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)

    def test_unverified_law_article_warns_without_blocking(self):
        proc, out = run_check("依《民法典》第159条，应视为条件已成就。")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("MC-LAW-UNCITED", [f["rule"] for f in out["findings"]])

    def test_contract_clause_numbers_do_not_warn(self):
        proc, out = run_check("按合同第三条验收，第五条付款。")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(out["findings"], [])

    def test_statute_in_same_sentence_as_contract_still_warns(self):
        proc, out = run_check("按合同第三条及《民法典》第159条主张价款。")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("MC-LAW-UNCITED", [f["rule"] for f in out["findings"]])

    def test_public_id_prefix_does_not_match_private_id(self):
        documents = json.loads(json.dumps(DOCUMENTS, ensure_ascii=False))
        documents.append({**documents[0], "id": "P-0010"})
        proc, out = run_check("材料P-0010显示合同约定。", documents=documents, citations=["P-0010"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(out["findings"], [])
        proc, out = run_check("材料P-001显示合同约定。", documents=documents)
        self.assertEqual(proc.returncode, 1)

    def test_long_private_text_tail_is_checked(self):
        documents = json.loads(json.dumps(DOCUMENTS, ensure_ascii=False))
        documents[3]["text"] = "合成备忘上下文。" * 8000 + "尾部独有事项共计二十七笔待核对记录。"
        proc, out = run_check("尾部独有事项共计二十七笔待核对记录。", documents=documents)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("MC-PRIVATE-TEXT", [f["rule"] for f in out["findings"]])

    def test_law_article_present_in_public_material_is_not_flagged(self):
        documents = json.loads(json.dumps(DOCUMENTS, ensure_ascii=False))
        documents[0]["text"] += "双方确认适用第159条。"
        proc, out = run_check("依公开材料所载第159条，应视为条件已成就。",
                              documents=documents)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("MC-LAW-UNCITED", [f["rule"] for f in out["findings"]])

    def test_unnamed_rule_proposition_passes(self):
        proc, out = run_check("原告主张：一方为自身利益不正当阻止条件成就时，可能产生相应法律后果（待核验）。")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(out["findings"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
