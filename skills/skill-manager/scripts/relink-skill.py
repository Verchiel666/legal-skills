#!/usr/bin/env python3
"""Skill 改名后的本机软链收敛工具。

场景：skill 目录改名/迁移后（如 multi-agent-orchestration → multi-agent-orca），
本机用户级与项目级 Agent 配置目录里仍残留指向旧路径的符号链接（悬空）。
本工具在有界扫描根内找出这些链接并批量规划/执行修复。

只处理符号链接，不移动任何实体文件；默认 dry-run，--apply 才执行。

用法：
  python3 relink-skill.py --old /path/to/old-skill-dir --new /path/to/new-skill-dir
  python3 relink-skill.py --old ... --new ... --apply --report /tmp/relink-report.json
  python3 relink-skill.py --old ... --new ... --exclude-glob '*/visualizations/*'
"""
import argparse
import fnmatch
import json
import os
import sys
import time

DEFAULT_ROOTS = [
    "~/.agents/skills",
    "~/.claude",
    "~/.codex",
    "~/.zcode",
    "~/Library/Application Support/maoscripts",
    "~/orca/workspaces",
]
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".Trash"}


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", required=True, help="旧技能目录绝对路径（链接命中前缀）")
    ap.add_argument("--new", required=True, help="新技能目录绝对路径（修复后的指向）")
    ap.add_argument("--roots", action="append", default=[], help="追加扫描根（可多次）；路径以 ~ 开头会展开")
    ap.add_argument("--roots-only", action="store_true", help="只用 --roots 提供的根，忽略内置默认根")
    ap.add_argument("--exclude-glob", action="append", default=[],
                    help="fnmatch 排除模式（可多次），命中的链接跳过，如 '*/visualizations/*'")
    ap.add_argument("--apply", action="store_true", help="执行修复（默认 dry-run 只打印计划）")
    ap.add_argument("--report", help="把扫描与修复结果写为 JSON 报告（追溯用）")
    ap.add_argument("--quiet", action="store_true", help="不打印逐条进度")
    return ap.parse_args()


def expand(path):
    return os.path.abspath(os.path.expanduser(path))


def resolve_target(link_path, target):
    """相对链接按链接所在目录解析为绝对路径。"""
    if target.startswith("/"):
        return os.path.normpath(target)
    return os.path.normpath(os.path.join(os.path.dirname(link_path), target))


def iter_symlinks(roots, quiet):
    for root in roots:
        root = expand(root)
        if not os.path.isdir(root):
            if not quiet:
                print(f"  跳过不存在根: {root}", file=sys.stderr)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # 指向目录的软链出现在 dirnames（walk 默认不深入），必须单独检查，
            # 否则 skills 安装链这类目录级链接全部漏扫。
            for d in list(dirnames):
                if os.path.islink(os.path.join(dirpath, d)):
                    yield os.path.join(dirpath, d)
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                p = os.path.join(dirpath, name)
                if os.path.islink(p):
                    yield p


def classify(link_path, target, old, new):
    """对一条命中链接给出修复动作。

    - basename 等于旧技能名（skills 安装链）：
        目标父目录已有新名链接/目录 → remove_old；否则 → remove_old + create_new
    - basename 不同（如 night-watch.sh、指向 config/references 子路径）：
        链接名保持，target 中 old 前缀替换为 new（repoint）
    """
    base = os.path.basename(link_path.rstrip("/"))
    old_base = os.path.basename(old.rstrip("/"))
    new_base = os.path.basename(new.rstrip("/"))
    # 先解析到绝对再替换前缀，再按原链接形态输出：
    # 绝对链保持绝对；相对链转回相对，避免把本机绝对路径写进仓库内相对链接。
    resolved_old = resolve_target(link_path, target)
    resolved_new = resolved_old.replace(old, new, 1)
    if target.startswith("/"):
        new_target = os.path.normpath(resolved_new)
    else:
        new_target = os.path.relpath(resolved_new, os.path.dirname(link_path))
    if base == old_base:
        sibling = os.path.join(os.path.dirname(link_path), new_base)
        if os.path.lexists(sibling):
            return {"action": "remove_old", "link": link_path, "old_target": target,
                    "note": f"已有 {new_base}，只删旧链"}
        return {"action": "replace_named", "link": link_path, "old_target": target,
                "new_link": sibling, "new_target": new_target, "note": "删旧链+以新名建新链"}
    return {"action": "repoint", "link": link_path, "old_target": target,
            "new_target": new_target, "note": "链接名不变，重指新路径"}


def excluded(path, patterns):
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def main():
    args = parse_args()
    old = expand(args.old)
    new = expand(args.new)
    if not os.path.isdir(new):
        print(f"错误: 新路径不存在: {new}", file=sys.stderr)
        return 2
    if old == new:
        print("错误: --old 与 --new 相同", file=sys.stderr)
        return 2

    roots = [expand(r) for r in args.roots] if args.roots_only else \
            [expand(r) for r in DEFAULT_ROOTS] + [expand(r) for r in args.roots]
    # 旧目录已删除是常态（先退役再收敛）；扫描不要求它存在

    findings, skipped = [], []
    t0 = time.time()
    for link in iter_symlinks(roots, args.quiet):
        try:
            target = os.readlink(link)
        except OSError:
            continue
        resolved = resolve_target(link, target)
        if old == resolved or resolved.startswith(old + os.sep) or target.startswith(old + os.sep) or target == old:
            if excluded(link, args.exclude_glob):
                skipped.append({"link": link, "target": target, "reason": "exclude-glob"})
                continue
            item = classify(link, target, old, new)
            item["dangling"] = not os.path.exists(link)
            findings.append(item)

    results = []
    for item in findings:
        act = item["action"]
        if not args.apply:
            status = "PLAN"
        elif act == "remove_old":
            os.remove(item["link"]); status = "REMOVED"
        elif act == "replace_named":
            os.remove(item["link"])
            os.symlink(item["new_target"], item["new_link"]); status = "REPLACED"
        else:  # repoint
            os.remove(item["link"])
            os.symlink(item["new_target"], item["link"]); status = "REPOINTED"
        item["result"] = status
        results.append(item)
        if not args.quiet:
            mark = "悬空" if item["dangling"] else "活"
            extra = item.get("new_link") or item.get("new_target") or ""
            print(f"[{status}] {mark} {act} {item['link']} -> {extra}  ({item['note']})")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n{mode}: 命中 {len(findings)} 条（悬空 {sum(1 for i in findings if i['dangling'])}），"
          f"排除 {len(skipped)} 条，根 {len(roots)} 个，耗时 {time.time()-t0:.1f}s")
    if args.report:
        with open(args.report, "w") as f:
            json.dump({"old": old, "new": new, "mode": mode, "roots": roots,
                       "findings": findings, "skipped": skipped}, f, ensure_ascii=False, indent=2)
        print(f"报告: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
