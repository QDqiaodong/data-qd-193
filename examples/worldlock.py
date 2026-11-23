#!/usr/bin/env python3
"""挂点世界锁定交付示例：替换上游控制轨，挂点及其子树原地不动。

离线运行（仅标准库）：

    python3 examples/worldlock.py

包未安装时自动回退到仓库内 src/。编译与采样都是独立进程调用
`python -m rig_pose.*`，请求与交付件写在临时目录中，结束时清理。

场景（任务书规定）：静止根 root，挂点 prop 局部 x 平移 2，其下 tip 局部
x 平移 1；mix 顶点半权重绑根（局部点 x=1）、半权重绑 tip。
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ)
ENV['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + ENV.get('PYTHONPATH', '')
try:
    import rig_pose  # noqa: F401
except ImportError:
    sys.path.insert(0, str(ROOT / 'src'))

from fractions import Fraction

from rig_pose.animate import pose
from rig_pose.model import validate

IDENTITY = ['1', '0', '0', '1', '0', '0']


def source():
    return {
        'joints': [
            {'id': 'root', 'parent': None, 'local': list(IDENTITY)},
            {'id': 'prop', 'parent': 'root', 'local': ['1', '0', '0', '1', '2', '0']},
            {'id': 'tip', 'parent': 'prop', 'local': ['1', '0', '0', '1', '1', '0']}],
        'tracks': [],
        'vertices': [
            {'id': 'tipv', 'influences': [
                {'joint_id': 'tip', 'point': ['0', '0'], 'weight': '1'}]},
            {'id': 'mix', 'influences': [
                {'joint_id': 'root', 'point': ['1', '0'], 'weight': '0.5'},
                {'joint_id': 'tip', 'point': ['0', '0'], 'weight': '0.5'}]}],
        'edges': [['mix', 'tipv']],
        'sample_ticks': [0, 1]}


def edit(matrix_at_1):
    return {'joint_id': 'root', 'keys': [
        {'tick': 0, 'matrix': list(IDENTITY)},
        {'tick': 1, 'matrix': list(matrix_at_1)}]}


def lock(detach):
    return {'joint_id': 'prop', 'key_ticks': [0, 1], 'allow_detach': detach}


def world(source_data, joint, tick):
    joints, order, tracks, vertices, edges, _ = validate(source_data)
    return pose(joints, order, tracks, vertices, edges,
                Fraction(tick))['joint_world'][joint]


def main():
    tmp = tempfile.TemporaryDirectory()
    work = Path(tmp.name)

    def run(title, argv, expect_code=0):
        proc = subprocess.run([sys.executable, '-m', *argv],
                              capture_output=True, text=True, env=ENV)
        line = proc.stdout.strip() or proc.stderr.strip()
        print(f'== {title}\n   退出码 {proc.returncode}  {line}')
        if proc.returncode != expect_code:
            print(f'   预期退出码 {expect_code}，示例中止。')
            raise SystemExit(1)
        return proc

    def compile_request(body, name, expect_code=0):
        req = work / f'{name}-request.json'
        req.write_text(json.dumps(body, ensure_ascii=False), encoding='utf-8')
        proc = run(f'worldlock：{name}',
                   ['rig_pose.worldlock', '--request', str(req),
                    '--output', str(work / f'{name}-delivered.json')], expect_code)
        return proc, work / f'{name}-delivered.json'

    # 1) 替换根轨只推 x 平移 0→1：保留挂接可交付
    body = {'source': source(), 'edit': edit(['1', '0', '0', '1', '1', '0']),
            'locks': [lock(False)]}
    _, delivered_path = compile_request(body, 'kept')
    delivered = json.loads(delivered_path.read_text(encoding='utf-8'))
    track = [t for t in delivered['tracks'] if t['joint_id'] == 'prop'][0]
    print(f'   prop 补偿轨：{json.dumps(track["keys"], ensure_ascii=False)}')
    run('旧采样入口打开交付件',
        ['rig_pose.cli', '--source', str(delivered_path),
         '--output', str(work / 'pose-report.json')])
    # 子帧 t=1/2（不在采样表内）：锁下后代姿态与编辑前完全一致
    before = world(source(), 'tip', Fraction(1, 2))
    after = world(delivered, 'tip', Fraction(1, 2))
    assert before == after == ['1', '0', '0', '1', '3', '0'], (before, after)
    print(f'   子帧 t=1/2：tip 世界矩阵 {after}（与编辑前一致）')
    mix = pose(*validate(delivered)[:5], Fraction(1, 2))['vertices']['mix']
    print(f'   子帧 t=1/2：混合顶点 mix = {mix}（仍随根运动，原为 [\'2\', \'0\']）')

    # 2) 新根轨再让 x 缩放 1→2：保留挂接无法全程保持，禁止脱挂 → blocked
    body = {'source': source(), 'edit': edit(['2', '0', '0', '1', '1', '0']),
            'locks': [lock(False)]}
    compile_request(body, 'blocked')

    # 3) 同一编辑允许脱挂：交付脱挂的子树，锁下姿态仍全时域不变
    body['locks'] = [lock(True)]
    _, detached_path = compile_request(body, 'detached')
    detached = json.loads(detached_path.read_text(encoding='utf-8'))
    prop = [j for j in detached['joints'] if j['id'] == 'prop'][0]
    assert prop['parent'] is None, prop
    print('   prop 的 parent 已置 null（脱挂），tip 挂接不变')
    before = world(source(), 'prop', Fraction(1, 2))
    after = world(detached, 'prop', Fraction(1, 2))
    assert before == after == ['1', '0', '0', '1', '2', '0'], (before, after)
    print(f'   子帧 t=1/2：prop 世界矩阵 {after}（与编辑前一致）')

    print('交付示例完成。格式与判定规则见 docs/worldlock.md，边界测试见 tests/test_worldlock.py。')


if __name__ == '__main__':
    main()
