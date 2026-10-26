#!/usr/bin/env python3
"""index_claude_trace.py 的定向回归：解析真实 stream-json 结构、去重、冲突、未完成、坏JSON、来源绑定。

fixture 是最小合成结构；真实 CLI 产物测试在本机归档存在时执行，缺失时显式跳过。
入口末尾输出 MC_TRACE_COVERAGE JSON；MC_REQUIRE_REAL_TRACE=1 时真实用例未通过退出 3，
防止仅凭合成回归 exit 0 冒称真实日志已覆盖。MC027_REAL_TRACE 指定只读原件路径。
不启动 Agent/CLI、不联网、不读取凭证配置。
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parent
INDEXER = SCRIPTS / "index_claude_trace.py"
FIXTURE = SCRIPTS.parent / "assets" / "claude-stream.example.jsonl"
REPO_ROOT = SCRIPTS.parents[2]
REAL_TRACE = Path(os.environ.get(
    "MC027_REAL_TRACE",
    REPO_ROOT / ".claude" / "agent-sessions" / "mc-z-claude-g5-261002"
    / "runtime-261002" / "g2-stream.jsonl"))
REAL_TRACE_VERIFIED = False


def run_indexer(*args):
    return subprocess.run([sys.executable, "-B", str(INDEXER), *args],
                          capture_output=True, text=True, timeout=120, check=False)


class IndexClaudeTraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="index-trace-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.trace = self.tmp / "trace.jsonl"
        self.output = self.tmp / "index.json"
        shutil.copyfile(FIXTURE, self.trace)

    def run_on_fixture(self):
        proc = run_indexer("--trace", str(self.trace), "--output", str(self.output))
        index = json.loads(self.output.read_text(encoding="utf-8")) if self.output.exists() else None
        return proc, index

    def test_fixture_real_format_completed_call(self):
        proc, index = self.run_on_fixture()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(index["verdict"], "VERIFIED_CALLS")
        self.assertEqual(index["summary"]["completed"], 1)
        call = index["calls"][0]
        self.assertEqual(call["tool_name"], "Agent")
        self.assertEqual(call["tool_use_id"], "call_fixture_0001")
        self.assertEqual(call["status"], "completed")
        self.assertIn("FIXTURE_SUB_OK", call["result_preview"])

    def test_duplicate_stream_event_deduped(self):
        _, index = self.run_on_fixture()
        self.assertEqual(index["summary"]["subagent_calls_total"], 1)
        self.assertEqual(index["summary"]["duplicate_tool_use_events"], 1)
        self.assertEqual(index["summary"]["other_tool_use"], 1)

    def test_source_binding_lines_and_digest(self):
        _, index = self.run_on_fixture()
        call = index["calls"][0]
        self.assertEqual(call["line"], 3)
        self.assertEqual(call["result_line"], 5)
        digest = hashlib.sha256(self.trace.read_bytes()).hexdigest()
        self.assertEqual(index["trace_sha256"], digest)
        self.assertTrue(os.path.isabs(index["trace_path"]))

    def test_unfinished_call_not_completed(self):
        lines = self.trace.read_text(encoding="utf-8").splitlines()
        del lines[4]
        self.trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
        proc, index = self.run_on_fixture()
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(index["verdict"], "NOT_VERIFIED")
        self.assertEqual(index["calls"][0]["status"], "unfinished")
        self.assertEqual(index["summary"]["completed"], 0)
        self.assertIn("no_completed_subagent_calls", index["not_verified_reasons"])

    def test_conflicting_same_id_rejected(self):
        lines = self.trace.read_text(encoding="utf-8").splitlines()
        duplicate = json.loads(lines[3])
        duplicate["message"]["content"][0]["input"]["prompt"] = "冲突稿：与首次出现不一致。"
        lines[3] = json.dumps(duplicate, ensure_ascii=False)
        self.trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
        proc = run_indexer("--trace", str(self.trace), "--output", str(self.output))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("冲突", proc.stderr)
        self.assertFalse(self.output.exists())

    def test_conflicting_result_rejected(self):
        lines = self.trace.read_text(encoding="utf-8").splitlines()
        second = json.loads(lines[4])
        second["message"]["content"][0]["content"][0]["text"] = "冲突结果稿"
        lines.append(json.dumps(second, ensure_ascii=False))
        self.trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
        proc = run_indexer("--trace", str(self.trace), "--output", str(self.output))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("结果冲突", proc.stderr)

    def test_bad_json_line_reported_not_verified(self):
        lines = self.trace.read_text(encoding="utf-8").splitlines()
        lines.insert(2, "{not-json")
        self.trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
        proc, index = self.run_on_fixture()
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(index["summary"]["bad_json_lines"], 1)
        self.assertEqual(index["summary"]["bad_json_line_numbers"], [3])
        self.assertEqual(index["verdict"], "NOT_VERIFIED")

    def test_no_subagent_calls_bash_only(self):
        lines = self.trace.read_text(encoding="utf-8").splitlines()
        for i in (2, 3):
            event = json.loads(lines[i])
            event["message"]["content"][0]["name"] = "Bash"
            lines[i] = json.dumps(event, ensure_ascii=False)
        self.trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
        proc, index = self.run_on_fixture()
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(index["summary"]["subagent_calls_total"], 0)
        self.assertIn("no_subagent_calls_found", index["not_verified_reasons"])

    def test_task_tool_name_accepted(self):
        lines = self.trace.read_text(encoding="utf-8").splitlines()
        for i in (2, 3):
            event = json.loads(lines[i])
            event["message"]["content"][0]["name"] = "Task"
            lines[i] = json.dumps(event, ensure_ascii=False)
        self.trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
        proc, index = self.run_on_fixture()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(index["calls"][0]["tool_name"], "Task")
        self.assertEqual(index["summary"]["completed"], 1)

    def test_refuse_overwrite_existing_output(self):
        self.output.write_text("既有内容，不得覆盖\n", encoding="utf-8")
        proc = run_indexer("--trace", str(self.trace), "--output", str(self.output))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("拒绝", proc.stderr)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "既有内容，不得覆盖\n")

    def test_missing_trace_clear_error(self):
        proc = run_indexer("--trace", str(self.tmp / "nope.jsonl"), "--output", str(self.output))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("trace 文件不存在", proc.stderr)
        self.assertFalse(self.output.exists())

    def test_prompt_not_dumped_into_index(self):
        _, index = self.run_on_fixture()
        blob = json.dumps(index, ensure_ascii=False)
        self.assertNotIn("合成fixture提示词", blob)
        call = index["calls"][0]
        self.assertRegex(call["prompt_sha256"], r"^[0-9a-f]{64}$")
        self.assertGreater(call["prompt_chars"], 0)
        self.assertEqual(call["description"], "合成子代理探针")

    def test_string_tool_result_content_handled(self):
        lines = self.trace.read_text(encoding="utf-8").splitlines()
        result_event = json.loads(lines[4])
        block = result_event["message"]["content"][0]
        block["content"] = "字符串型返回：FIXTURE_SUB_OK"
        lines[4] = json.dumps(result_event, ensure_ascii=False)
        self.trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
        proc, index = self.run_on_fixture()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("FIXTURE_SUB_OK", index["calls"][0]["result_preview"])

    @unittest.skipUnless(REAL_TRACE.is_file(), "本机真实 Claude stream-json 归档不存在，跳过")
    def test_real_cli_trace_indexed(self):
        global REAL_TRACE_VERIFIED
        output = self.tmp / "real-index.json"
        proc = run_indexer("--trace", str(REAL_TRACE), "--output", str(output))
        index = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreaterEqual(index["summary"]["completed"], 1)
        self.assertEqual(index["calls"][0]["tool_name"], "Agent")
        self.assertEqual(index["final_result"]["subtype"], "success")
        # 新宿主可能在返回前加入长的来源包装，300 字预览不一定含固定短答。
        # 从绑定行号回查原件并核结果摘要，不能把预览截断当成真实调用失败。
        lines = REAL_TRACE.read_text(encoding="utf-8").splitlines()
        full_results = []
        for call in index["calls"]:
            if call["status"] != "completed":
                continue
            event = json.loads(lines[call["result_line"] - 1])
            block = next(block for block in event["message"]["content"]
                         if block.get("tool_use_id") == call["tool_use_id"])
            content = block["content"]
            text = content if isinstance(content, str) else "\n".join(
                item["text"] for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str))
            self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), call["result_sha256"])
            full_results.append(text)
        self.assertIn("MOOT_G2_SUB_OK", "\n".join(full_results))
        digest = hashlib.sha256(REAL_TRACE.read_bytes()).hexdigest()
        self.assertEqual(index["trace_sha256"], digest)
        self.assertTrue(index["session_ids"])
        REAL_TRACE_VERIFIED = True

    def test_missing_real_trace_coverage_contract(self):
        missing = self.tmp / "missing-native-trace.jsonl"
        env = dict(os.environ, MC027_REAL_TRACE=str(missing))
        selected = ["IndexClaudeTraceTest.test_fixture_real_format_completed_call",
                    "IndexClaudeTraceTest.test_real_cli_trace_indexed"]
        for strict, expected in (("0", 0), ("1", 3)):
            with self.subTest(strict=strict):
                env["MC_REQUIRE_REAL_TRACE"] = strict
                proc = subprocess.run([sys.executable, "-B", str(Path(__file__)), *selected],
                                      env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(proc.returncode, expected, proc.stdout + proc.stderr)
                line = next(line for line in proc.stdout.splitlines()
                            if line.startswith("MC_TRACE_COVERAGE "))
                coverage = json.loads(line.partition(" ")[2])
                self.assertEqual(coverage["real_trace"], "NOT_VERIFIED")
                self.assertEqual(coverage["skipped"], 1)
                self.assertTrue(coverage["suite_ok"])


if __name__ == "__main__":
    program = unittest.main(verbosity=2, exit=False)
    strict = os.environ.get("MC_REQUIRE_REAL_TRACE", "0")
    if strict not in ("0", "1"):
        print("MC_REQUIRE_REAL_TRACE 仅接受 0 或 1", file=sys.stderr)
        raise SystemExit(2)
    result = program.result
    print("MC_TRACE_COVERAGE " + json.dumps({
        "suite_ok": result.wasSuccessful(),
        "tests_run": result.testsRun,
        "skipped": len(result.skipped),
        "real_trace": "VERIFIED" if REAL_TRACE_VERIFIED else "NOT_VERIFIED",
        "require_real_trace": strict == "1",
    }, ensure_ascii=False, sort_keys=True))
    raise SystemExit(1 if not result.wasSuccessful()
                     else 3 if strict == "1" and not REAL_TRACE_VERIFIED else 0)
