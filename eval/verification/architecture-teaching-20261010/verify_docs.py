"""教学文档静态校验：不导入应用、不读凭据/买家库、不请求外部服务。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[3]
MAIN = ROOT / "docs/项目架构与实现原理教学.md"
INDEX = ROOT / "docs/组件与源码索引.md"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest():
    files = []
    for directory in ("app", "frontend/src", "configs", "scripts", "knowledge"):
        for path in sorted((ROOT / directory).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".yml", ".yaml", ".ts", ".tsx", ".css", ".md"}:
                files.append(path)
    files += [ROOT / name for name in ("pyproject.toml", "Makefile", "Dockerfile", "frontend/package.json")]
    entries = {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(files)}
    app_digest = hashlib.sha256()
    for path in sorted((ROOT / "app").rglob("*")):
        if path.is_file() and path.suffix in {".py", ".yml", ".yaml"}:
            app_digest.update(path.relative_to(ROOT / "app").as_posix().encode())
            app_digest.update(b"\0")
            app_digest.update(path.read_bytes())
            app_digest.update(b"\0")
    return {"app_source_sha256": app_digest.hexdigest(), "source_file_count": len(entries),
            "aggregate_sha256": hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest(),
            "files": entries}


def anchors(text: str):
    result, seen = set(), {}
    for line in text.splitlines():
        if not re.match(r"^#{1,6} ", line):
            continue
        title = re.sub(r"^#{1,6} ", "", line).lower()
        title = re.sub(r"[^\w\- ]", "", title).replace(" ", "-")
        count = seen.get(title, 0)
        seen[title] = count + 1
        result.add(title if not count else f"{title}-{count}")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(Path(__file__).resolve().parent):
        raise ValueError("输出只能写入本次证据目录")
    output.mkdir(parents=True, exist_ok=False)
    manifest = source_manifest()
    (output / "source-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    errors, checks, linked = [], [], set()
    documents = [MAIN, INDEX]
    # 根 README 只校验本次新增教学链接，既有文档引用不扩展为本次修复范围。
    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    documents += sorted((ROOT / "docs/改动记录/2026-10-10").glob("*.md"))
    for doc in documents:
        content = doc.read_text(encoding="utf-8")
        fences = sum(line.startswith("```") for line in content.splitlines())
        if fences % 2:
            errors.append({"file": str(doc), "error": "代码围栏未闭合"})
        if "\ufffd" in content:
            errors.append({"file": str(doc), "error": "存在 Unicode 替换字符"})
        targets = re.findall(r"\[[^\]\n]+\]\(([^)\n]+)\)", content)
        for target in targets:
            if target.startswith(("https:", "http:")):
                continue
            pathname, _, fragment = unquote(target).partition("#")
            resolved = (doc.parent / pathname).resolve() if pathname else doc
            if not resolved.exists():
                errors.append({"file": doc.relative_to(ROOT).as_posix(), "target": target, "error": "本地链接不存在"})
                continue
            linked.add(resolved)
            if fragment and resolved.suffix == ".md" and fragment not in anchors(resolved.read_text(encoding="utf-8")):
                errors.append({"file": doc.relative_to(ROOT).as_posix(), "target": target, "error": "标题锚点不存在"})
        checks.append({"document": doc.relative_to(ROOT).as_posix(), "sha256": sha(doc),
                       "lines": len(content.splitlines()), "characters": len(content), "links": len(targets)})
    index_content = INDEX.read_text(encoding="utf-8")
    index_links = {(INDEX.parent / t).resolve() for t in re.findall(r"\[[^\]\n]+\]\(([^)\n]+)\)", index_content)
                   if not t.startswith(("#", "https:", "http:"))}
    expected = [p for p in (ROOT / "app").rglob("*.py") if p.name != "__init__.py"]
    expected += list((ROOT / "frontend/src").rglob("*.ts")) + list((ROOT / "frontend/src").rglob("*.tsx"))
    missing = sorted(p.relative_to(ROOT).as_posix() for p in expected if p.resolve() not in index_links)
    if missing:
        errors.append({"error": "组件源码索引未覆盖", "files": missing})
    entry_ok = all(path in root_readme for path in ("docs/项目架构与实现原理教学.md", "docs/组件与源码索引.md"))
    if not entry_ok:
        errors.append({"error": "根 README 教学入口缺失"})
    date_index = (ROOT / "docs/改动记录/README.md").read_text(encoding="utf-8")
    if "2026-10-10/README.md" not in date_index:
        errors.append({"error": "日期总索引缺失"})
    unchanged = None
    if args.baseline:
        previous = json.loads(args.baseline.read_text(encoding="utf-8"))
        unchanged = previous["files"] == manifest["files"]
        if not unchanged:
            errors.append({"error": "两次文档校验之间源码发生变化"})
    report = {"stage": args.stage, "verified_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
              "validation_kind": "static_document_validation", "passed": not errors,
              "documents": checks, "component_files_expected": len(expected),
              "component_files_covered": len(expected) - len(missing), "missing_components": missing,
              "root_readme_entries": entry_ok, "source_unchanged_since_baseline": unchanged,
              "app_source_sha256": manifest["app_source_sha256"], "errors": errors,
              "not_run": ["业务单测", "前端构建", "真实模型调用", "外部链路", "Harness smoke/release", "生产效果验收"],
              "limits": ["检查覆盖、链接、锚点、围栏与源码指纹；不证明所有教学论述正确", "未检查根 README 的既有非本次链接", "Mermaid 语法和图形布局未由本脚本渲染"]}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    (output / "report.json").write_text(rendered, encoding="utf-8")
    (output / "raw-validation.log").write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
