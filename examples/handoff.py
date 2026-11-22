#!/usr/bin/env python3
"""按帧交接示例：发布 fixtures/input.json，经历领取、交付、修订保留与还原重派。

离线运行（仅标准库）：

    python3 examples/handoff.py

包未安装时自动回退到仓库内 src/。每个操作都是独立进程调用
`python -m rig_pose.spool`，spool 目录建在临时目录中，结束时清理。
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'input.json'
ENV = dict(os.environ)
ENV['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + ENV.get('PYTHONPATH', '')


def load_source():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def revised_source():
    """只为 root 插入 tick 5、平移 x=6 的关键帧（其他系数同原轨道）。"""
    data = load_source()
    data['tracks'][0]['keys'].insert(1, {'tick': 5, 'matrix': ['1', '0', '0', '1', '6', '0']})
    return data


def main():
    tmp = tempfile.TemporaryDirectory()
    work = Path(tmp.name)
    spool = work / 'spool'
    counter = 0

    def step(title, request):
        nonlocal counter
        counter += 1
        path = work / f'request-{counter}.json'
        path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        proc = subprocess.run(
            [sys.executable, '-m', 'rig_pose.spool',
             '--spool', str(spool), '--request', str(path)],
            capture_output=True, text=True, env=ENV)
        if proc.returncode != 0:
            print(f'== {title}\n   退出码 {proc.returncode}: {proc.stderr.strip()}')
            raise SystemExit(1)
        response = json.loads(proc.stdout)
        print(f'== {title}\n   {json.dumps(response, ensure_ascii=False, sort_keys=True)}')
        return response

    src = load_source()
    step('发布样例（版本 r1）', {'op': 'revise', 'op_id': 'r1', 'base': None, 'source': src})
    step('领取 tick 0', {'op': 'claim', 'op_id': 'c0', 'tick': 0})
    step('交付 tick 0 产物', {'op': 'complete', 'op_id': 'done0', 'claim_id': 'c0',
                            'artifact_sha256': 'a' * 64})
    step('领取 tick 5', {'op': 'claim', 'op_id': 'c5', 'tick': 5})
    step('领取 tick 10', {'op': 'claim', 'op_id': 'c10', 'tick': 10})
    step('修订：root 插入 tick5 x=6 关键帧（版本 r2）',
         {'op': 'revise', 'op_id': 'r2', 'base': 'r1', 'source': revised_source()})
    step('tick 10 的旧领取仍可交付', {'op': 'complete', 'op_id': 'done10', 'claim_id': 'c10',
                                    'artifact_sha256': 'b' * 64})
    step('tick 5 旧凭据已随世代退休', {'op': 'complete', 'op_id': 'done5late', 'claim_id': 'c5',
                                     'artifact_sha256': 'c' * 64})
    claimed = step('重新领取 tick 5（blend 的 x 已为 7）',
                   {'op': 'claim', 'op_id': 'c5b', 'tick': 5})
    assert claimed['payload']['vertices']['blend'] == ['7', '0'], claimed
    step('还原原输入（版本 r3），tick 5 再开世代',
         {'op': 'revise', 'op_id': 'r3', 'base': 'r2', 'source': src})
    step('r2 世代的领取凭据交付 → stale_claim',
         {'op': 'complete', 'op_id': 'done5b', 'claim_id': 'c5b', 'artifact_sha256': 'd' * 64})
    step('原领取请求重试 → 同一份旧线稿', {'op': 'claim', 'op_id': 'c5', 'tick': 5})
    step('当前交接视图', {'op': 'status'})
    print('交接示例完成。崩溃与并发边界见 tests/test_spool.py。')


if __name__ == '__main__':
    main()
