#!/usr/bin/env python3
"""入卷前机械预检：私有材料泄露、非公开引用与未核验法条编号。

只做可重复的字符串与集合比对，不判断证明力、不评价观点强弱、
不判断法条是否真的适用。语义判定仍由主控与独立复核承担。

预检报告只给出私有材料的序号与命中长度，不回显私有编号、标题或原文；
主控不得把报告粘贴到 cancel --reason、角色上下文或公开笔录。
"""
import argparse
import json
import re
import sys
from pathlib import Path

PRIVATE_ID = "MC-PRIVATE-ID"
PRIVATE_TEXT = "MC-PRIVATE-TEXT"
PRIVATE_CITATION = "MC-PRIVATE-CITATION"
LAW_UNCITED = "MC-LAW-UNCITED"

# 只认「第X条」与「第X条第Y款」。「第X项」在正文里常指列表、问题或段落编号，
# 误报远多于收益，故不纳入。
LAW_ARTICLE = re.compile(
    r"第[零一二三四五六七八九十百千0-9]{1,6}条(?:第[零一二三四五六七八九十百千0-9]{1,6}款)?")
CONTRACT_CONTEXT = ("合同", "协议", "约定", "条款", "本约", "合约", "契约")
SHINGLE = 10
MAX_FINDINGS_PER_RULE = 5


def fail(message):
    print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(2)


def read_json(path, label):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"{label}无法读取完整 JSON：{path}（{type(exc).__name__}）")


def validate_documents(documents, label):
    if not isinstance(documents, list):
        fail(f"{label}.documents 必须是数组")
    ids = set()
    for index, doc in enumerate(documents, start=1):
        if not isinstance(doc, dict):
            fail(f"{label} 第 {index} 项不是对象")
        for key in ("id", "title", "text"):
            if key not in doc:
                fail(f"{label} 第 {index} 项缺少 {key}")
            if not isinstance(doc[key], str) or not doc[key].strip():
                fail(f"{label} 第 {index} 项 {key} 必须为非空文本")
        if doc["id"] in ids:
            fail(f"{label} 材料编号重复")
        ids.add(doc["id"])
        audience = doc.get("visible_to")
        if (not isinstance(audience, list) or not audience or
                not all(isinstance(role, str) and role for role in audience)):
            fail(f"{label} 第 {index} 项 visible_to 必须是非空文本数组")
        if len(set(audience)) != len(audience) or ("all" in audience and audience != ["all"]):
            fail(f"{label} 第 {index} 项 visible_to 公开标记或重复角色不合法")


def validate_packet(packet):
    if not isinstance(packet, dict):
        fail("packet 顶层必须是 JSON 对象")
    validate_documents(packet.get("documents"), "packet")
    assignment = packet.get("assignment")
    if assignment is not None and not isinstance(assignment, dict):
        fail("packet.assignment 必须是对象")


def validate_manifest(manifest):
    if not isinstance(manifest, dict):
        fail("manifest 顶层必须是 JSON 对象")
    validate_documents(manifest.get("documents"), "manifest")


def validate_submission(submission):
    if not isinstance(submission, dict):
        fail("submission 顶层必须是 JSON 对象")
    body = submission.get("body")
    if not isinstance(body, str) or not body.strip():
        fail("submission.body 必须是非空文本")
    citations = submission.get("citations", [])
    if not isinstance(citations, list) or not all(isinstance(c, str) for c in citations):
        fail("submission.citations 必须是文本数组")


def bind_packet_and_submission(packet, submission):
    """packet 与提交稿必须属于同一回合；避免用陈旧 packet 得到无意义结果。"""
    assignment = packet.get("assignment")
    if not isinstance(assignment, dict):
        fail("packet 缺少 assignment")
    for key in ("schema_version", "run_id", "turn_id", "role", "material_version",
                "read_through", "responds_to"):
        if key not in assignment or key not in submission:
            fail(f"packet 或 submission 缺少派发字段：{key}")
        if type(submission[key]) is not type(assignment[key]) or submission[key] != assignment[key]:
            fail(f"packet 与 submission 的 {key} 不一致；请使用同一回合的原样 packet")


def bind_manifest(packet, manifest):
    role = packet["assignment"]["role"]
    expected = [doc for doc in manifest["documents"]
                if doc["visible_to"] == ["all"] or role in doc["visible_to"]]
    expected_by_id = {doc["id"]: doc for doc in expected}
    actual_by_id = {doc["id"]: doc for doc in packet["documents"]}
    if actual_by_id != expected_by_id:
        fail("manifest 与本轮 packet 材料不一致；请使用派发时的完整材料快照")


def split_visibility(documents):
    """公开稿只以 visible_to == ["all"] 为可引用范围。

    多方可见不等于 ["all"]；公开稿会经 history 传播到全体角色。
    要披露这类材料，先更新完整清单为 ["all"] 并重派，沿用书记员的引用规则。
    """
    public, private = [], []
    for doc in documents:
        (public if doc.get("visible_to") == ["all"] else private).append(doc)
    return public, private


def compact(text):
    return re.sub(r"\s+", "", text)


def shingles(text, size=SHINGLE):
    return {text[i:i + size] for i in range(max(0, len(text) - size + 1))}


def find_private_ids(body, private_docs):
    hits = []
    for index, doc in enumerate(private_docs, start=1):
        doc_id = doc["id"]
        # 编号按完整 token 比对，P-001 不误伤公开编号 P-0010。标题常是通用词，
        # 不再作为机械硬阻断条件。
        pattern = r"(?<![A-Za-z0-9_-])" + re.escape(doc_id) + r"(?![A-Za-z0-9_-])"
        if re.search(pattern, body):
            hits.append({"doc_ref": f"private_doc_{index}", "matched_chars": len(doc_id)})
    return hits[:MAX_FINDINGS_PER_RULE]


def find_private_text(body, private_docs, public_shingles):
    body_compact = compact(body)
    body_shingles = shingles(body_compact)
    hits = []
    for index, doc in enumerate(private_docs, start=1):
        # 仅比较私有材料独有的 10 字窗口，避免备忘引用公开原文时被误报。
        # 集合比对随文本长度增长，不截断长材料以换取速度。
        private_windows = shingles(compact(doc["text"])) - public_shingles
        if private_windows & body_shingles:
            hits.append({"doc_ref": f"private_doc_{index}", "matched_chars": SHINGLE})
    return hits[:MAX_FINDINGS_PER_RULE]


def find_private_citations(citations, public_ids):
    hits = []
    for index, citation in enumerate(citations, start=1):
        if citation not in public_ids:
            hits.append({"citation_ref": f"citation_{index}"})
    return hits[:MAX_FINDINGS_PER_RULE]


def find_unverified_law(body, public_compact):
    body_compact = compact(body)
    hits = set()
    for match in LAW_ARTICLE.finditer(body_compact):
        token = match.group(0)
        sentence_start = max(body_compact.rfind(mark, 0, match.start())
                             for mark in "。；!?") + 1
        prefix = body_compact[max(sentence_start, match.start() - 20):match.start()]
        if any(word in prefix for word in CONTRACT_CONTEXT) and not re.search(
                r"《[^》]+》|(?:法|条例|规定|解释)(?:第[零一二三四五六七八九十百千0-9]+条)?[^。；!?]*$",
                prefix):
            continue
        if token not in public_compact:
            hits.add(token)
    return sorted(hits)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="入卷前机械预检：私有材料泄露、非公开引用与未核验法条编号"
                    "（不判断法律语义；报告不回显私有内容）")
    parser.add_argument("--packet", required=True, help="dispatch 返回的原样 packet JSON")
    parser.add_argument("--manifest", required=True,
                        help="本次运行当前完整材料清单；仅凭角色 packet 无法发现他方私有材料")
    parser.add_argument("--submission", required=True, help="角色提交稿 JSON")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)

    try:
        packet = read_json(args.packet, "packet")
        manifest = read_json(args.manifest, "manifest")
        submission = read_json(args.submission, "submission")
        validate_packet(packet)
        validate_manifest(manifest)
        validate_submission(submission)
        bind_packet_and_submission(packet, submission)
        bind_manifest(packet, manifest)

        public_docs, private_docs = split_visibility(manifest["documents"])
        public_compact = compact("\n".join(doc["text"] for doc in public_docs))
        public_shingles = set()
        for doc in public_docs:
            public_shingles.update(shingles(compact(doc["text"])))
        public_ids = {doc["id"] for doc in public_docs}
        body = submission["body"]

        findings = []
        for hit in find_private_ids(body, private_docs):
            findings.append({
                "rule": PRIVATE_ID,
                "severity": "block",
                "detail": f"公开稿出现 {hit['doc_ref']} 的标识形态"
                          f"（{hit['matched_chars']} 字）；具体编号不回显",
            })
        for hit in find_private_text(body, private_docs, public_shingles):
            findings.append({
                "rule": PRIVATE_TEXT,
                "severity": "block",
                "detail": f"公开稿命中 {hit['doc_ref']} 的私有原文"
                          f"（至少 {hit['matched_chars']} 字）；原文不回显",
            })
        for hit in find_private_citations(submission.get("citations", []), public_ids):
            findings.append({
                "rule": PRIVATE_CITATION,
                "severity": "block",
                "detail": f"citations 的 {hit['citation_ref']} 指向非公开或未知材料编号；编号不回显",
            })
        for token in find_unverified_law(body, public_compact):
            findings.append({
                "rule": LAW_UNCITED,
                "severity": "warn",
                "detail": f"法条编号「{token}」未见于任何公开材料，案卷内没有可核验的法源原文",
            })

        blocked = [finding for finding in findings if finding["severity"] == "block"]
        result = {
            "ok": not blocked,
            "blocked": len(blocked),
            "warnings": len(findings) - len(blocked),
            "public_docs_considered": len(public_docs),
            "private_docs_considered": len(private_docs),
            "findings": findings,
            "note": "仅机械比对；不判断证明力、法条适用或观点强弱。"
                    "报告已隐去私有编号与原文，不得粘贴进公开笔录、cancel 原因或角色上下文。",
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for finding in findings:
                mark = "阻断" if finding["severity"] == "block" else "提示"
                print(f"[{mark}] {finding['rule']}：{finding['detail']}")
            print("通过：未发现阻断项" if not blocked else f"未通过：{len(blocked)} 项阻断")
        return 1 if blocked else 0
    except SystemExit:
        raise
    except (TypeError, AttributeError, KeyError, ValueError, OSError) as exc:
        fail(f"输入结构损坏或无法处理：{type(exc).__name__}")


if __name__ == "__main__":
    sys.exit(main())
