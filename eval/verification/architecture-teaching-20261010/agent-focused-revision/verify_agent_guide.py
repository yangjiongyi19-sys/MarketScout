"""静态检查 Agent 教学呈现与合成算例，不运行应用或模型。"""
import hashlib
import json
import math
from pathlib import Path
import re


def main():
    root = Path(__file__).resolve().parents[4]
    guide = root / 'docs/项目架构与实现原理教学.md'
    content = guide.read_text(encoding='utf-8')
    equations = re.findall(r'^\$\$\n(.*?)\n\$\$', content, re.MULTILINE | re.DOTALL)
    brace_errors = []
    for index, equation in enumerate(equations, 1):
        balance = 0
        for match in re.finditer(r'(?<!\\)[{}]', equation):
            balance += 1 if match.group() == '{' else -1
            if balance < 0:
                break
        if balance:
            brace_errors.append(index)
    targets = re.findall(r'\[[^\]\n]+\]\(([^)\n]+)\)', content)
    checks = {
        'chapters_22': len(re.findall(r'^## \d+\.', content, re.MULTILINE)) == 22,
        'no_source_code_or_command_examples': '`' not in content and '~~~' not in content
            and re.search(r'app/|frontend/|\.py\b|\.tsx?\b|python\s+-|make\s+eval|curl\s', content) is None,
        'only_internal_heading_navigation': all(t.startswith('#') for t in targets),
        'math_delimiters_balanced': content.splitlines().count('$$') == 2 * len(equations),
        'math_braces_balanced': not brace_errors,
        'at_least_15_equation_blocks': len(equations) >= 15,
        'model_message_roles_and_io': all(s in content for s in ['System', 'User', 'Assistant', 'Tool', 'call-A']),
        'seven_step_dialogue_and_state': all(s in content for s in ['第一步', '第二步', '第三步', '第四步', '第五步', '第六步', '第七步', '切换任务分支']),
        'synthetic_examples_explicit': '合成预期路径' in content and '不是实测' in content,
        'backend_out_of_learning_scope': '先不展开接口开发、数据库实现、服务部署、消息队列和前端组件' in content,
    }
    def cosine(left, right):
        return sum(a * b for a, b in zip(left, right)) / (math.hypot(*left) * math.hypot(*right))
    calculations = {
        'cosine_A': {'actual': cosine((1, 0), (.8, .6)), 'expected': .8, 'tolerance': 1e-9},
        'cosine_B': {'actual': cosine((1, 0), (.6, .8)), 'expected': .6, 'tolerance': 1e-9},
        'RRF_A': {'actual': 1 / 61 + 1 / 70, 'expected': .03068, 'tolerance': .000005},
        'RRF_B': {'actual': 2 / 62, 'expected': .03226, 'tolerance': .000005},
        'NDCG_two_items': {'actual': 1 / math.log2(3), 'expected': .6309, 'tolerance': .00005},
        'input_capacity': {'actual': 32768 - 4096 - 1024, 'expected': 27648, 'tolerance': 0},
        'working_capacity': {'actual': min(27648, 24000), 'expected': 24000, 'tolerance': 0},
        'two_items_merchandise_total': {'actual': 2 * 220, 'expected': 440, 'tolerance': 0},
        'serial_seconds': {'actual': 4 + 6, 'expected': 10, 'tolerance': 0},
        'parallel_seconds': {'actual': max(4, 6) + 1, 'expected': 7, 'tolerance': 0},
    }
    for entry in calculations.values():
        entry['passed'] = abs(entry['actual'] - entry['expected']) <= entry['tolerance']
    report = {
        'validation_kind': 'static_agent_guide_and_synthetic_arithmetic',
        'passed': all(checks.values()) and all(c['passed'] for c in calculations.values()),
        'document': guide.relative_to(root).as_posix(),
        'sha256': hashlib.sha256(guide.read_bytes()).hexdigest(),
        'lines': len(content.splitlines()),
        'characters': len(content),
        'equation_blocks': len(equations),
        'checks': checks,
        'unbalanced_equation_indices': brace_errors,
        'synthetic_arithmetic': calculations,
        'not_run': ['真实多轮模型链路', '应用回归', 'Harness 评测', '页面与数学排版渲染', '生产效果验收'],
        'limits': ['公式仅检查围栏与花括号，不等于完整 LaTeX 语法验证', '算例为合成数据，不证明检索或 Agent 运行效果', '技术机制由本次人工核对支持'],
    }
    output = Path(__file__).resolve().parent / 'agent-guide-check.json'
    if output.exists():
        raise FileExistsError('保留已有证据，不覆盖')
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    output.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
