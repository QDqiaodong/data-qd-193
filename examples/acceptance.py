#!/usr/bin/env python3
"""安全框验收示例：动画方 check 出凭证，合成方 verify 独立验收。

离线运行（仅标准库）：

    python3 examples/acceptance.py

包未安装时自动回退到仓库内 src/。每个操作都是独立进程调用
`python -m rig_pose.crop`，请求与报告写在临时目录中，结束时清理。

场景（任务书规定）：静止根 + 四级连续缩放链（各级 tick 0→1 将 x 缩放
0→1，y 不变、无偏移）。顶点绑第二级（局部 x=-16，权 0.25）、第四级
（x=16，权 0.25）与静止根（x=2，权 0.5），局部 y 全 0，只采样 0、1。
顶点 x(t) = (2t²-1)²：两端 x=1，中间恰贴左边 0 但不越界。
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ)
ENV['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + ENV.get('PYTHONPATH', '')


def chain_source():
    joints = [{'id': 'root', 'parent': None, 'local': ['1', '0', '0', '1', '0', '0']}]
    tracks = []
    for i in range(1, 5):
        parent = 'root' if i == 1 else f'c{i - 1}'
        joints.append({'id': f'c{i}', 'parent': parent, 'local': ['1', '0', '0', '1', '0', '0']})
        tracks.append({'joint_id': f'c{i}', 'keys': [
            {'tick': 0, 'matrix': ['0', '0', '0', '1', '0', '0']},
            {'tick': 1, 'matrix': ['1', '0', '0', '1', '0', '0']}]})
    return {'joints': joints, 'tracks': tracks,
            'vertices': [{'id': 'v', 'influences': [
                {'joint_id': 'c2', 'point': ['-16', '0'], 'weight': '0.25'},
                {'joint_id': 'c4', 'point': ['16', '0'], 'weight': '0.25'},
                {'joint_id': 'root', 'point': ['2', '0'], 'weight': '0.5'}]}],
            'edges': [], 'sample_ticks': [0, 1]}


def request_body(left):
    return {'source': chain_source(),
            'viewport': [
                {'tick': 0, 'min': [left, '-1'], 'max': ['2', '1']},
                {'tick': 1, 'min': [left, '-1'], 'max': ['2', '1']}]}


def main():
    tmp = tempfile.TemporaryDirectory()
    work = Path(tmp.name)
    counter = 0

    def run(title, argv, expect_code=0):
        proc = subprocess.run([sys.executable, '-m', 'rig_pose.crop', *argv],
                              capture_output=True, text=True, env=ENV)
        line = proc.stdout.strip() or proc.stderr.strip()
        print(f'== {title}\n   退出码 {proc.returncode}  {line}')
        if proc.returncode != expect_code:
            print(f'   预期退出码 {expect_code}，示例中止。')
            raise SystemExit(1)
        return proc

    def write_request(body, name):
        path = work / name
        path.write_text(json.dumps(body, ensure_ascii=False), encoding='utf-8')
        return path

    # 1) 左边为 0：中间恰贴边不越界 → safe 凭证，合成方接受
    req_a = write_request(request_body('0'), 'request-a.json')
    run('check：左边 0（中间恰贴边）',
        ['check', '--request', str(req_a), '--output', str(work / 'report-a.json')])
    run('verify：合成方独立核验 → 接受',
        ['verify', '--request', str(req_a), '--report', str(work / 'report-a.json')])

    # 2) 左边抬到 0.25：端点仍合格，子帧内越界 → unsafe + 可核验见证
    req_b = write_request(request_body('0.25'), 'request-b.json')
    run('check：左边抬高到 0.25',
        ['check', '--request', str(req_b), '--output', str(work / 'report-b.json')])
    report_b = json.loads((work / 'report-b.json').read_text(encoding='utf-8'))
    print(f'   见证：{json.dumps(report_b["credential"]["witness"], ensure_ascii=False, sort_keys=True)}')
    run('verify：确认越界属实（不代表可投产）',
        ['verify', '--request', str(req_b), '--report', str(work / 'report-b.json')])

    # 3) 篡改见证时刻（换成端点）→ verify 拒绝
    forged = json.loads(json.dumps(report_b))
    forged['credential']['witness']['tick'] = '0'
    (work / 'forged-tick.json').write_text(json.dumps(forged), encoding='utf-8')
    run('verify：见证时刻被换成端点 → 拒绝',
        ['verify', '--request', str(req_b), '--report', str(work / 'forged-tick.json')],
        expect_code=2)

    # 4) 旧 safe 凭证搬到收紧后的安全框（重填匹配摘要）→ verify 拒绝
    report_a = json.loads((work / 'report-a.json').read_text(encoding='utf-8'))
    moved = json.loads(json.dumps(report_a))
    moved['request_sha256'] = hashlib.sha256(req_b.read_bytes()).hexdigest()
    (work / 'moved.json').write_text(json.dumps(moved), encoding='utf-8')
    run('verify：旧凭证搬到收紧安全框（已更新摘要）→ 拒绝',
        ['verify', '--request', str(req_b), '--report', str(work / 'moved.json')],
        expect_code=2)

    # 5) safe 证明删掉一项必要覆盖 → verify 拒绝
    stripped = json.loads(json.dumps(report_a))
    constraints = stripped['credential']['segments'][0]['constraints']
    stripped['credential']['segments'][0]['constraints'] = constraints[1:]
    (work / 'stripped.json').write_text(json.dumps(stripped), encoding='utf-8')
    run('verify：证明缺一项覆盖 → 拒绝',
        ['verify', '--request', str(req_a), '--report', str(work / 'stripped.json')],
        expect_code=2)

    print('验收示例完成。格式与核验规则见 docs/crop.md，边界测试见 tests/test_crop.py。')


if __name__ == '__main__':
    main()
