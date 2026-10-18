"""只校验教学正文的呈现约束，不证明技术结论或教学效果。"""
import hashlib
import json
from pathlib import Path
import re


def main():
    root = Path(__file__).resolve().parents[4]
    guide = root / 'docs/项目架构与实现原理教学.md'
    content = guide.read_text(encoding='utf-8')
    targets = re.findall(r'\[[^\]\n]+\]\(([^)\n]+)\)', content)
    checks = {
        'no_code_fences_or_inline_code': '`' not in content and '~~~' not in content,
        'links_only_internal_heading_navigation': all(t.startswith('#') for t in targets),
        'no_source_paths_or_command_examples': re.search(
            r'app/|frontend/|\.py\b|\.tsx?\b|python\s+-|make\s+eval|curl\s|[A-Z]:\\', content
        ) is None,
        'chapter_count_22': len(re.findall(r'^## \d+\.', content, re.MULTILINE)) == 22,
        'no_replacement_characters': '\ufffd' not in content,
    }
    report = {
        'validation_kind': 'static_natural_language_presentation_check',
        'passed': all(checks.values()),
        'document': guide.relative_to(root).as_posix(),
        'sha256': hashlib.sha256(guide.read_bytes()).hexdigest(),
        'characters': len(content),
        'lines': len(content.splitlines()),
        'checks': checks,
        'limits': ['只检查明确呈现约束；技术正确性由人工核对支持', '不验证真实链路、页面排版或学习效果'],
    }
    output = Path(__file__).resolve().parent / 'language-check.json'
    if output.exists():
        raise FileExistsError('保留已有证据，不覆盖')
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    output.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
