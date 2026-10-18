"""修订版项目/JD交集教学指南的静态核对，不调用业务或模型。"""
import argparse, hashlib, json, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
GUIDE = ROOT / "docs/求职准备/岗位JD技术覆盖教学指南.md"

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def fingerprint():
    paths = []
    for folder in ("app", "frontend/src", "configs", "scripts", "knowledge", "tests", "frontend/tests"):
        paths.extend(p for p in (ROOT / folder).rglob("*") if p.is_file()
                     and p.suffix in {".py", ".ts", ".tsx", ".css", ".json", ".yml", ".yaml", ".md"})
    paths.extend(ROOT / p for p in ("pyproject.toml", "Makefile", "Dockerfile", "frontend/package.json"))
    files = {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(paths)}
    h = hashlib.sha256()
    for p in sorted((ROOT / "app").rglob("*")):
        if p.is_file() and p.suffix in {".py", ".yml", ".yaml"}:
            h.update(p.relative_to(ROOT / "app").as_posix().encode())
            h.update(b"\0"); h.update(p.read_bytes()); h.update(b"\0")
    return {"app_source_sha256": h.hexdigest(), "files": files, "source_file_count": len(files)}

def anchors(text):
    seen, found = {}, set()
    for line in text.splitlines():
        if re.match(r"^#{1,6} ", line):
            value = re.sub(r"[^\w\- ]", "", re.sub(r"^#{1,6} ", "", line).lower()).replace(" ", "-")
            n = seen.get(value, 0); seen[value] = n + 1
            found.add(value if not n else f"{value}-{n}")
    return found

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True)
    stage = parser.parse_args().stage
    out = EVIDENCE / stage
    if out.parent != EVIDENCE or out.exists():
        raise ValueError("每阶段必须用新的独立目录")
    out.mkdir()
    guide = GUIDE.read_text(encoding="utf-8")
    current = fingerprint()
    baseline = json.loads((EVIDENCE.parent / "scope-baseline/source-manifest.json").read_text(encoding="utf-8"))
    coverage = json.loads((EVIDENCE / "project-intersection.json").read_text(encoding="utf-8"))
    errors = []
    chapters = re.findall(r"^## (\d+)\. ", guide, re.M)
    if chapters != [str(i) for i in range(1, 20)]:
        errors.append("章节未按1至19连续编号")
    current_chapter = None
    for line in guide.splitlines():
        main = re.match(r"^## (\d+)\. ", line)
        sub = re.match(r"^#{3,6} (\d+)\.\d+", line)
        if main:
            current_chapter = int(main.group(1))
        elif sub and int(sub.group(1)) != current_chapter:
            errors.append("子标题编号与所在章节不一致：" + line)
    if len(guide) < 12000 or guide.count("练习与参考要点") < 12:
        errors.append("正文长度或练习覆盖不足")
    if chr(96) in guide or chr(96)*3 in guide:
        errors.append("发现源码或命令格式")
    if "\\n## " in guide:
        errors.append("章节被字面反斜杠n拼接")
    for heading in re.findall(r"^## \d+\. (.+)$", guide, re.M):
        if re.search(r"强化学习|PPO|DPO|GRPO|DAPO|GSPO|SFT|MCP|机器人|多模态|图检索|AIOps|C\+\+|PyTorch", heading, re.I):
            errors.append("项目外技术存在教学章节：" + heading)
    blocks = re.findall(r"\$\$(.*?)\$\$", guide, re.S)
    if guide.count("$$") % 2:
        errors.append("公式定界符未配对")
    for index, block in enumerate(blocks, 1):
        depth = 0
        for char in block:
            depth += (char == "{") - (char == "}")
            if depth < 0:
                errors.append(f"第{index}个公式括号顺序错误"); break
        if depth:
            errors.append(f"第{index}个公式花括号未配对")
    if not all(name in guide for name in ("JD1", "JD2", "JD3", "JD4", "JD5", "Multi-Agent", "Memory")):
        errors.append("岗位或交集覆盖不完整")
    for topic in coverage["intersections"]:
        if not topic["jds"] or any(not 1 <= c <= 19 for c in topic["chapters"]):
            errors.append("交集映射缺岗位或章节：" + topic["topic"])
        for path in topic["evidence"]:
            if not (ROOT / path).exists():
                errors.append("源码证据不存在：" + path)
    docs = []
    doc_list = [GUIDE, ROOT / "README.md", ROOT / "docs/改动记录/2026-10-10/README.md",
                ROOT / "docs/改动记录/2026-10-10/岗位JD技术覆盖教学指南.md",
                ROOT / "docs/改动记录/README.md"]
    for doc in doc_list:
        text = doc.read_text(encoding="utf-8")
        for link in re.findall(r"\[[^\]\n]+\]\(([^)\n]+)\)", text):
            if link.startswith(("http:", "https:")):
                continue
            path, _, fragment = unquote(link).partition("#")
            target = (doc.parent / path).resolve() if path else doc
            if not target.exists():
                errors.append(f"失效链接：{doc.relative_to(ROOT)} → {link}")
            elif fragment and target.suffix == ".md" and fragment not in anchors(target.read_text(encoding="utf-8")):
                errors.append("失效标题锚点：" + link)
        if "\ufffd" in text:
            errors.append("编码异常：" + doc.name)
        docs.append({"path": doc.relative_to(ROOT).as_posix(), "sha256": sha(doc),
                     "characters": len(text), "lines": len(text.splitlines())})
    if current != baseline:
        errors.append("代码、配置或测试文件指纹发生变化")
    inputs = {p.name: sha(p) for p in (ROOT / "eval/verification/jd-teaching-20261010/jd-inputs").glob("*.png")}
    if len(inputs) != 5:
        errors.append("原始岗位截图证据数量不是5")
    report = {"stage": stage, "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "kind": "static_project_jd_intersection_validation", "passed": not errors, "errors": errors,
              "chapters": len(chapters), "intersection_topics": len(coverage["intersections"]),
              "math_blocks": len(blocks), "characters": len(guide), "lines": len(guide.splitlines()),
              "source_file_count": current["source_file_count"], "app_source_sha256": current["app_source_sha256"],
              "documents": docs, "jd_input_sha256": inputs,
              "not_run": ["业务回归", "真实模型调用", "模型训练", "Harness评测", "外部服务效果", "公式页面渲染", "教学效果验收"]}
    raw = json.dumps(report, ensure_ascii=False, indent=2)
    (out / "report.json").write_text(raw, encoding="utf-8")
    (out / "raw-validation.log").write_text(raw + "\n", encoding="utf-8")
    (out / "guide-snapshot.md").write_text(guide, encoding="utf-8")
    (out / "source-manifest.json").write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    print(raw)
    return int(bool(errors))

if __name__ == "__main__":
    raise SystemExit(main())

