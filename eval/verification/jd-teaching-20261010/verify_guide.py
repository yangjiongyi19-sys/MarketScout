"""岗位教学文档静态核对；不导入业务、不读取凭据或买家数据、不调用模型。"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = Path(__file__).resolve().parent
GUIDE = ROOT / 'docs/求职准备/岗位JD技术覆盖教学指南.md'

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def fingerprint():
    files = []
    for folder in ('app', 'frontend/src', 'configs', 'scripts', 'knowledge', 'tests', 'frontend/tests'):
        files += [p for p in (ROOT / folder).rglob('*') if p.is_file() and p.suffix in {'.py', '.ts', '.tsx', '.css', '.json', '.yml', '.yaml', '.md'}]
    files += [ROOT / p for p in ('pyproject.toml', 'Makefile', 'Dockerfile', 'frontend/package.json')]
    entries = {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(files)}
    digest = hashlib.sha256()
    for p in sorted((ROOT / 'app').rglob('*')):
        if p.is_file() and p.suffix in {'.py', '.yml', '.yaml'}:
            digest.update(p.relative_to(ROOT / 'app').as_posix().encode())
            digest.update(b'\0')
            digest.update(p.read_bytes())
            digest.update(b'\0')
    return {'app_source_sha256': digest.hexdigest(), 'files': entries, 'source_file_count': len(entries)}

def anchors(content):
    seen, found = {}, set()
    for line in content.splitlines():
        if re.match(r'^#{1,6} ', line):
            a = re.sub(r'[^\w\- ]', '', re.sub(r'^#{1,6} ', '', line).lower()).replace(' ', '-')
            n = seen.get(a, 0)
            seen[a] = n + 1
            found.add(a if not n else f'{a}-{n}')
    return found

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', required=True)
    args = parser.parse_args()
    out = EVIDENCE / args.stage
    if out.parent != EVIDENCE or out.exists():
        raise ValueError('每阶段使用新的直接子目录')
    out.mkdir()
    manifest = fingerprint()
    (out / 'source-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    errors, documents, presentation, arithmetic = [], [], {}, []
    if args.stage != 'baseline':
        content = GUIDE.read_text(encoding='utf-8')
        chapters = re.findall(r'^## (\d+)\. ', content, re.M)
        if chapters != [str(n) for n in range(1, 31)]:
            errors.append('30章编号不连续')
        if content.count('$$') % 2:
            errors.append('数学块未闭合')
        blocks = re.findall(r'\$\$(.*?)\$\$', content, re.S)
        for index, block in enumerate(blocks, 1):
            depth = 0
            for char in block:
                depth += (char == '{') - (char == '}')
                if depth < 0:
                    errors.append(f'数学块{index}花括号顺序不合法')
                    break
            if depth:
                errors.append(f'数学块{index}花括号未配对')
        if '```' in content or '`' in content:
            errors.append('主指南含代码围栏或行内代码，不符合自然语言教学约定')
        coverage = json.loads((EVIDENCE / 'coverage.json').read_text(encoding='utf-8'))
        if [r['id'] for r in coverage] != [f'T{n:02d}' for n in range(1, 36)]:
            errors.append('35项技术覆盖编号不连续')
        for row in coverage:
            if row['id'] not in content or not 1 <= row['chapter'] <= 30 or not row['jds']:
                errors.append('JD覆盖映射缺失：' + row['id'])
        table_rows = re.findall(r'^\| (T\d{2}) \| ([^|]+) \| ([^|]+) \| (\d+) \|', content, re.M)
        parsed = {i: (t.strip(), [int(n) for n in re.findall(r'\d+', j)], int(c)) for i, t, j, c in table_rows}
        for row in coverage:
            if parsed.get(row['id']) != (row['topic'], row['jds'], row['chapter']):
                errors.append('指南技术表与覆盖清单不一致：' + row['id'])
        if len(content) < 30000:
            errors.append('教程不足30000字符，请核对详细程度')
        if content.count('练习') < 25:
            errors.append('练习覆盖不足')
        presentation = {'chapters': len(chapters), 'coverage_topics': len(coverage), 'math_blocks': len(blocks), 'characters': len(content), 'lines': len(content.splitlines()), 'exercise_markers': content.count('练习'), 'note': '字符数包含标点和Markdown；不代表汉字数、渲染验证或学习效果'}
        calculations = [
            ('并发查询顺序耗时', 2 + 3, 5, 1e-12),
            ('专家并发含派发汇总', .5 + max(2, 3) + .5, 4, 1e-12),
            ('余弦相似度', (3 * 4 + 4 * 3) / (5 * 5), .96, 1e-12),
            ('RRF A', 1 / 61 + 1 / 63, .032266, .0000005),
            ('RRF B', 2 / 62, .032258, .0000005),
            ('SFT 0.1概率负对数', -math.log(.1), 2.303, .0005),
            ('SFT 0.5概率负对数', -math.log(.5), .693, .0005),
            ('LoRA基础参数', 4096 * 4096, 16777216, 1e-12),
            ('LoRA低秩参数', 8 * 4096 * 2, 65536, 1e-12),
            ('LoRA比例百分数', 65536 / 16777216 * 100, .39, .005),
            ('DPO算例损失', -math.log(2 / 3), .405, .0005),
            ('GRPO总体标准差', math.sqrt(sum((x - .5) ** 2 for x in [0, 1, 1, 0]) / 4), .5, 1e-12),
            ('GSPO序列归一化比', math.exp((math.log(4) + math.log(.25)) / 2), 1, 1e-12),
            ('梯度累积有效批量', 4 * 8, 32, 1e-12),
        ]
        arithmetic = [{'name': name, 'recalculated': actual, 'stated': expected, 'tolerance': tol, 'passed': abs(actual - expected) <= tol} for name, actual, expected, tol in calculations]
        errors += ['合成算例复算失败：' + row['name'] for row in arithmetic if not row['passed']]
        doc_paths = [GUIDE, ROOT / 'docs/改动记录/2026-10-10/岗位JD技术覆盖教学指南.md', ROOT / 'docs/改动记录/2026-10-10/README.md', ROOT / 'docs/改动记录/README.md']
        for doc in doc_paths:
            c = doc.read_text(encoding='utf-8')
            if '\ufffd' in c:
                errors.append('字符编码异常：' + doc.name)
            links = re.findall(r'\[[^\]\n]+\]\(([^)\n]+)\)', c)
            for link in links:
                if link.startswith(('https:', 'http:')):
                    continue
                path, _, fragment = unquote(link).partition('#')
                target = (doc.parent / path).resolve() if path else doc
                if not target.exists():
                    errors.append('链接不存在：' + doc.name + ' → ' + link)
                elif fragment and target.suffix == '.md' and fragment not in anchors(target.read_text(encoding='utf-8')):
                    errors.append('标题锚点不存在：' + link)
            documents.append({'path': doc.relative_to(ROOT).as_posix(), 'sha256': sha(doc), 'characters': len(c), 'lines': len(c.splitlines())})
        if 'docs/求职准备/岗位JD技术覆盖教学指南.md' not in (ROOT / 'README.md').read_text(encoding='utf-8'):
            errors.append('根README教学入口缺失')
        previous = json.loads((EVIDENCE / 'baseline/source-manifest.json').read_text(encoding='utf-8'))
        if previous != manifest:
            errors.append('应用、配置、测试等源码与文档修改前不同')
        (out / 'guide-snapshot.md').write_text(content, encoding='utf-8')
    inputs = {p.name: sha(p) for p in (EVIDENCE / 'jd-inputs').glob('*.png')}
    report = {'stage': args.stage, 'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(), 'kind': 'static_document_validation', 'passed': not errors, 'errors': errors, 'presentation': presentation, 'arithmetic_checks': arithmetic, 'app_source_sha256': manifest['app_source_sha256'], 'source_file_count': manifest['source_file_count'], 'documents': documents, 'jd_input_sha256': inputs, 'not_run': ['业务回归', '模型训练', '真实模型请求', 'Harness评测', '项目外部服务可用性', '页面公式渲染', '教学效果验收']}
    raw = json.dumps(report, ensure_ascii=False, indent=2)
    (out / 'report.json').write_text(raw, encoding='utf-8')
    (out / 'raw-validation.log').write_text(raw + '\n', encoding='utf-8')
    print(raw)
    return int(bool(errors))

if __name__ == '__main__':
    raise SystemExit(main())
