import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction

import rig_pose
from rig_pose.animate import pose
from rig_pose.model import validate
from rig_pose.service import process
from rig_pose.worldlock import main

PACKAGE_PARENT = str(Path(rig_pose.__file__).resolve().parents[1])
IDENTITY = ['1', '0', '0', '1', '0', '0']


def scenario1_source():
    """静止根 + 挂点子关节（局部 x 平移 2）+ 其下 tip（局部 x 平移 1）。

    mix 顶点同时绑定被编辑根（局部点 x=1）与锁下 tip，用来检验混合绑定。
    """
    return {
        'joints': [
            {'id': 'root', 'parent': None, 'local': IDENTITY},
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


def edit_root(matrix_at_1):
    """替换根轨：tick 0→1 从单位矩阵到给定矩阵，之外按旧规则持值。"""
    return {'joint_id': 'root', 'keys': [
        {'tick': 0, 'matrix': list(IDENTITY)},
        {'tick': 1, 'matrix': list(matrix_at_1)}]}


def lock(joint, ticks, detach):
    return {'joint_id': joint, 'key_ticks': list(ticks), 'allow_detach': detach}


def frame(source, tick):
    """用旧采样管线在任意（可有理数）时刻求姿态。"""
    joints, order, tracks, vertices, edges, _ = validate(source)
    return pose(joints, order, tracks, vertices, edges, Fraction(tick))


class WorldlockCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.count = 0

    def write(self, body=None, raw=None, name=None):
        self.count += 1
        path = self.root / (name or f'req-{self.count}.json')
        if raw is None:
            path.write_text(json.dumps(body, ensure_ascii=False), encoding='utf-8')
        elif isinstance(raw, bytes):
            path.write_bytes(raw)
        else:
            path.write_text(raw, encoding='utf-8')
        return path

    def read(self, path):
        return json.loads(path.read_text(encoding='utf-8'))

    def call(self, request_path, output_name='out.json'):
        output = self.root / output_name
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--request', str(request_path), '--output', str(output)])
        return code, out.getvalue(), err.getvalue(), output

    def compile_ok(self, request_path, output_name='out.json'):
        code, out, err, output = self.call(request_path, output_name)
        self.assertEqual(code, 0, f'stderr: {err}')
        self.assertEqual(err, '')
        report = json.loads(out)
        self.assertEqual(report['status'], 'compiled')
        self.assertTrue(output.exists(), 'compiled must create the output source')
        return report, self.read(output), output

    def blocked_ok(self, request_path, output_name='out.json'):
        code, out, err, output = self.call(request_path, output_name)
        self.assertEqual(code, 0, f'stderr: {err}')
        report = json.loads(out)
        self.assertEqual(report['status'], 'blocked')
        self.assertFalse(output.exists(), 'blocked must not create any output')
        return report

    def bad_request(self, body=None, raw=None):
        request_path = self.write(body, raw=raw)
        code, out, err, output = self.call(request_path)
        self.assertEqual(code, 2, f'stdout: {out}')
        self.assertEqual(out, '')
        self.assertIn('error:', err)
        self.assertFalse(output.exists(), 'failed request must not create output')
        return err

    def assert_worlds_match(self, original, delivered, joint, ticks):
        for t in ticks:
            before = frame(original, t)['joint_world'][joint]
            after = frame(delivered, t)['joint_world'][joint]
            self.assertEqual(before, after, f'{joint} world differs at tick {t}')


class KeptDeliveryTests(WorldlockCase):
    """任务场景一：根静止、锁定子关节局部 x 平移 2，替换根轨 x 平移 0→1。"""

    def request(self, detach):
        return {'source': scenario1_source(),
                'edit': edit_root(['1', '0', '0', '1', '1', '0']),
                'locks': [lock('prop', [0, 1], detach)]}

    def test_kept_delivery_holds_world_matrix_everywhere(self):
        req = self.write(self.request(detach=False))
        report, delivered, _ = self.compile_ok(req)
        self.assertEqual(report['locks'], {'prop': 'kept'})
        self.assertEqual(report['options'], {'prop': ['kept']})
        # 挂接保留：parent 不动，只写补偿轨
        prop = {j['id']: j for j in delivered['joints']}['prop']
        self.assertEqual(prop['parent'], 'root')
        self.assertEqual(prop['local'], ['1', '0', '0', '1', '2', '0'])
        track = {t['joint_id']: t for t in delivered['tracks']}['prop']
        self.assertEqual([k['tick'] for k in track['keys']], [0, 1])
        self.assertEqual([k['matrix'] for k in track['keys']],
                         [['1', '0', '0', '1', '2', '0'],
                          ['1', '0', '0', '1', '1', '0']])
        # 其余资料原样保留：其他关节、顶点、边、采样表
        source = scenario1_source()
        self.assertEqual(delivered['vertices'], source['vertices'])
        self.assertEqual(delivered['edges'], source['edges'])
        self.assertEqual(delivered['sample_ticks'], source['sample_ticks'])
        for j in source['joints']:
            if j['id'] != 'prop':
                self.assertIn(j, delivered['joints'])
        # 全时域锁定：采样表内外的子帧都核对锁下后代姿态
        self.assert_worlds_match(source, delivered, 'prop',
                                 [0, Fraction(1, 4), Fraction(1, 2),
                                  Fraction(3, 4), 1, 2, 999999, 1000000])
        self.assert_worlds_match(source, delivered, 'tip',
                                 [0, Fraction(1, 2), 1, 500000])

    def test_kept_delivery_samples_with_old_entry(self):
        """交付件直接过旧采样入口；采样帧与子帧中的锁定世界矩阵不变。"""
        req = self.write(self.request(detach=False))
        _, delivered, output = self.compile_ok(req)
        result = process(delivered)  # 旧 Python 入口
        self.assertEqual(result['summary']['joints'], 3)
        for f in result['frames']:
            self.assertEqual(f['joint_world']['prop'], ['1', '0', '0', '1', '2', '0'])
            self.assertEqual(f['joint_world']['tip'], ['1', '0', '0', '1', '3', '0'])
        # 未列入原采样表的子帧：锁下后代姿态保持，混合顶点仍随根运动
        sub = frame(delivered, Fraction(1, 2))
        self.assertEqual(sub['joint_world']['prop'], ['1', '0', '0', '1', '2', '0'])
        self.assertEqual(sub['joint_world']['tip'], ['1', '0', '0', '1', '3', '0'])
        self.assertEqual(sub['vertices']['tipv'], ['3', '0'])
        # 根 x 平移 1/2、根局部点 x=1 → 根贡献 3/2；mix = 3/4 + 3/2 = 9/4
        self.assertEqual(sub['vertices']['mix'], ['9/4', '0'])
        original_sub = frame(scenario1_source(), Fraction(1, 2))
        self.assertEqual(original_sub['vertices']['mix'], ['2', '0'])
        self.assertEqual(sub['edge_segments'][0]['end'], ['3', '0'])
        self.assertEqual(sub['bbox']['max'], ['3', '0'])

    def test_both_forms_available_when_detach_allowed(self):
        """允许脱挂时两种形态均成立；按确定规则交付保留挂接。"""
        req = self.write(self.request(detach=True))
        report, delivered, _ = self.compile_ok(req)
        self.assertEqual(report['locks'], {'prop': 'kept'})
        self.assertEqual(report['options'], {'prop': ['kept', 'detached']})
        prop = {j['id']: j for j in delivered['joints']}['prop']
        self.assertEqual(prop['parent'], 'root')

    def test_edit_mid_chain_joint_with_new_track(self):
        """被编辑关节在链中间且原本无轨道：新增轨道，补偿照常唯一确定。"""
        source = {
            'joints': [
                {'id': 'root', 'parent': None, 'local': list(IDENTITY)},
                {'id': 'mid', 'parent': 'root', 'local': ['1', '0', '0', '1', '10', '0']},
                {'id': 'prop', 'parent': 'mid', 'local': ['1', '0', '0', '1', '2', '0']}],
            'tracks': [],
            'vertices': [{'id': 'pv', 'influences': [
                {'joint_id': 'prop', 'point': ['0', '0'], 'weight': '1'}]}],
            'edges': [], 'sample_ticks': [0, 1]}
        request = {'source': source,
                   'edit': {'joint_id': 'mid', 'keys': [
                       {'tick': 0, 'matrix': ['1', '0', '0', '1', '10', '0']},
                       {'tick': 1, 'matrix': ['1', '0', '0', '1', '11', '0']}]},
                   'locks': [lock('prop', [0, 1], False)]}
        req = self.write(request)
        report, delivered, _ = self.compile_ok(req)
        self.assertEqual(report['locks'], {'prop': 'kept'})
        self.assertEqual(len(delivered['tracks']), 2)  # 新增编辑轨 + 补偿轨
        track = {t['joint_id']: t for t in delivered['tracks']}['prop']
        self.assertEqual([k['matrix'] for k in track['keys']],
                         [['1', '0', '0', '1', '2', '0'],
                          ['1', '0', '0', '1', '1', '0']])
        self.assert_worlds_match(source, delivered, 'prop',
                                 [0, Fraction(1, 2), 1, 2, 1000000])


class ScaleBlocksKeepTests(WorldlockCase):
    """任务场景一扩展：新根轨再让 x 缩放 1→2，保留挂接无法全程保持。"""

    def request(self, detach):
        return {'source': scenario1_source(),
                'edit': edit_root(['2', '0', '0', '1', '1', '0']),
                'locks': [lock('prop', [0, 1], detach)]}

    def test_keep_blocked_even_though_both_keys_match(self):
        """两个键时刻都能对上，但区间中间对不上 → 无精确解。"""
        req = self.write(self.request(detach=False))
        report = self.blocked_ok(req)
        self.assertEqual(report['blocked'], {'prop': {'kept': 'no_exact_solution'}})

    def test_detach_allowed_delivers_detached_subtree(self):
        req = self.write(self.request(detach=True))
        report, delivered, _ = self.compile_ok(req)
        self.assertEqual(report['locks'], {'prop': 'detached'})
        self.assertEqual(report['options'], {'prop': ['detached']})
        prop = {j['id']: j for j in delivered['joints']}['prop']
        self.assertIsNone(prop['parent'])  # 脱挂：parent 置 null
        tip = {j['id']: j for j in delivered['joints']}['tip']
        self.assertEqual(tip['parent'], 'prop')  # 后代挂接不变
        track = {t['joint_id']: t for t in delivered['tracks']}['prop']
        self.assertEqual([k['matrix'] for k in track['keys']],
                         [['1', '0', '0', '1', '2', '0'],
                          ['1', '0', '0', '1', '2', '0']])
        # 锁下后代全时域保持原世界姿态
        self.assert_worlds_match(scenario1_source(), delivered, 'prop',
                                 [0, Fraction(1, 2), 1, 2, 1000000])
        self.assert_worlds_match(scenario1_source(), delivered, 'tip',
                                 [Fraction(1, 2), 1, 123457])
        # 混合顶点仍正确反映根的运动（缩放+平移后的根贡献为 2）
        sub = frame(delivered, Fraction(1, 2))
        self.assertEqual(sub['vertices']['mix'], ['5/2', '0'])
        self.assertEqual(sub['vertices']['tipv'], ['3', '0'])

    def test_delivery_form_actually_changes_hierarchy(self):
        """可交付形态改变时，交付件确实使用对应层级。"""
        kept_req = self.write({'source': scenario1_source(),
                               'edit': edit_root(['1', '0', '0', '1', '1', '0']),
                               'locks': [lock('prop', [0, 1], True)]})
        _, kept, _ = self.compile_ok(kept_req, 'kept.json')
        detach_req = self.write(self.request(detach=True))
        _, detached, _ = self.compile_ok(detach_req, 'detached.json')
        parents = lambda d: {j['id']: j['parent'] for j in d['joints']}
        self.assertEqual(parents(kept)['prop'], 'root')
        self.assertIsNone(parents(detached)['prop'])
        # 两种形态下锁的世界矩阵都等于编辑前的同一个目标
        for t in [Fraction(1, 3), Fraction(1, 2), 1]:
            self.assertEqual(frame(kept, t)['joint_world']['prop'],
                             frame(detached, t)['joint_world']['prop'])


class ReverseScenarioTests(WorldlockCase):
    """任务场景二：原根与锁定子关节 x 缩放均 1→2，替换根轨只加 y 平移。"""

    def source(self):
        data = scenario1_source()
        data['joints'][1]['local'] = list(IDENTITY)
        data['tracks'] = [
            {'joint_id': 'root', 'keys': [
                {'tick': 0, 'matrix': list(IDENTITY)},
                {'tick': 1, 'matrix': ['2', '0', '0', '1', '0', '0']}]},
            {'joint_id': 'prop', 'keys': [
                {'tick': 0, 'matrix': list(IDENTITY)},
                {'tick': 1, 'matrix': ['2', '0', '0', '1', '0', '0']}]}]
        return data

    def test_kept_preferred_even_when_detach_allowed(self):
        request = {'source': self.source(),
                   'edit': edit_root(['2', '0', '0', '1', '0', '1']),
                   'locks': [lock('prop', [0, 1], True)]}
        req = self.write(request)
        report, delivered, _ = self.compile_ok(req)
        # 脱挂无精确解（世界 x 缩放是二次式），保留挂接唯一可交付
        self.assertEqual(report['locks'], {'prop': 'kept'})
        self.assertEqual(report['options'], {'prop': ['kept']})
        track = {t['joint_id']: t for t in delivered['tracks']}['prop']
        self.assertEqual([k['matrix'] for k in track['keys']],
                         [list(IDENTITY), ['2', '0', '0', '1', '0', '-1']])
        # 中间时刻世界 x 缩放是 9/4，不是首尾复制的 5/2
        sub = frame(delivered, Fraction(1, 2))
        self.assertEqual(sub['joint_world']['prop'][0], '9/4')
        self.assert_worlds_match(self.source(), delivered, 'prop',
                                 [0, Fraction(1, 4), Fraction(1, 2),
                                  Fraction(3, 4), 1, 2, 1000000])
        self.assert_worlds_match(self.source(), delivered, 'tip',
                                 [Fraction(1, 2), 1, 42])
        # 混合顶点仍反映根新增的 y 运动
        self.assertEqual(sub['vertices']['mix'], ['15/8', '1/4'])
        original_sub = frame(self.source(), Fraction(1, 2))
        self.assertEqual(original_sub['vertices']['mix'], ['15/8', '0'])


class SingularityTests(WorldlockCase):
    """孤立奇异时刻不得拒解；整段退化不得任意填零发布。"""

    def test_isolated_singular_key_still_has_unique_compensation(self):
        """父矩阵在键时刻 t=0 奇异，但全时域补偿仍唯一确定。"""
        source = {
            'joints': [
                {'id': 'root', 'parent': None, 'local': list(IDENTITY)},
                {'id': 'prop', 'parent': 'root', 'local': list(IDENTITY)}],
            'tracks': [
                {'joint_id': 'root', 'keys': [
                    {'tick': 0, 'matrix': ['0', '0', '0', '1', '0', '0']},
                    {'tick': 1, 'matrix': list(IDENTITY)}]},
                {'joint_id': 'prop', 'keys': [
                    {'tick': 0, 'matrix': list(IDENTITY)},
                    {'tick': 1, 'matrix': ['1', '0', '0', '1', '2', '0']}]}],
            'vertices': [{'id': 'pv', 'influences': [
                {'joint_id': 'prop', 'point': ['0', '0'], 'weight': '1'}]}],
            'edges': [], 'sample_ticks': [0, 1]}
        request = {'source': source,
                   'edit': {'joint_id': 'root', 'keys': [
                       {'tick': 0, 'matrix': ['0', '0', '0', '1', '0', '0']},
                       {'tick': 1, 'matrix': ['1', '0', '0', '1', '0', '1']}]},
                   'locks': [lock('prop', [0, 1], True)]}
        req = self.write(request)
        report, delivered, _ = self.compile_ok(req)
        self.assertEqual(report['locks'], {'prop': 'kept'})
        track = {t['joint_id']: t for t in delivered['tracks']}['prop']
        self.assertEqual([k['matrix'] for k in track['keys']],
                         [list(IDENTITY), ['1', '0', '0', '1', '2', '-1']])
        # 奇异点 t=0 也在闭区间内，世界矩阵必须处处相等
        self.assert_worlds_match(source, delivered, 'prop',
                                 [0, Fraction(1, 4), Fraction(1, 2), 1, 2])
        # 奇异姿态是正常诊断，不阻止交付件被旧入口采样
        result = process(delivered)
        self.assertEqual(result['frames'][0]['singular_joint_ids'], ['prop', 'root'])

    def test_segment_degenerate_parent_is_not_unique(self):
        """父矩阵整段为零矩阵：补偿不唯一，禁止脱挂时必须 blocked。"""
        zero = ['0', '0', '0', '0', '0', '0']
        source = {
            'joints': [
                {'id': 'root', 'parent': None, 'local': list(IDENTITY)},
                {'id': 'prop', 'parent': 'root', 'local': ['1', '0', '0', '1', '2', '0']}],
            'tracks': [{'joint_id': 'root', 'keys': [
                {'tick': 0, 'matrix': list(zero)},
                {'tick': 1, 'matrix': list(zero)},
                {'tick': 2, 'matrix': list(IDENTITY)}]}],
            'vertices': [{'id': 'pv', 'influences': [
                {'joint_id': 'prop', 'point': ['0', '0'], 'weight': '1'}]}],
            'edges': [], 'sample_ticks': [0, 2]}
        edit = {'joint_id': 'root', 'keys': [
            {'tick': 0, 'matrix': list(zero)},
            {'tick': 1, 'matrix': list(zero)},
            {'tick': 2, 'matrix': ['1', '0', '0', '1', '1', '0']}]}
        req = self.write({'source': source, 'edit': edit,
                          'locks': [lock('prop', [0, 1, 2], False)]})
        report = self.blocked_ok(req)
        self.assertEqual(report['blocked'], {'prop': {'kept': 'not_unique'}})

    def test_segment_degenerate_detachable_still_unique(self):
        """同一段退化，脱挂形态仍被世界矩阵唯一钉死（含零矩阵键）。"""
        zero = ['0', '0', '0', '0', '0', '0']
        source = {
            'joints': [
                {'id': 'root', 'parent': None, 'local': list(IDENTITY)},
                {'id': 'prop', 'parent': 'root', 'local': ['1', '0', '0', '1', '2', '0']}],
            'tracks': [{'joint_id': 'root', 'keys': [
                {'tick': 0, 'matrix': list(zero)},
                {'tick': 1, 'matrix': list(zero)},
                {'tick': 2, 'matrix': list(IDENTITY)}]}],
            'vertices': [{'id': 'pv', 'influences': [
                {'joint_id': 'prop', 'point': ['0', '0'], 'weight': '1'}]}],
            'edges': [], 'sample_ticks': [0, 2]}
        edit = {'joint_id': 'root', 'keys': [
            {'tick': 0, 'matrix': list(zero)},
            {'tick': 1, 'matrix': list(zero)},
            {'tick': 2, 'matrix': ['1', '0', '0', '1', '1', '0']}]}
        req = self.write({'source': source, 'edit': edit,
                          'locks': [lock('prop', [0, 1, 2], True)]})
        report, delivered, _ = self.compile_ok(req)
        self.assertEqual(report['locks'], {'prop': 'detached'})
        track = {t['joint_id']: t for t in delivered['tracks']}['prop']
        self.assertEqual([k['matrix'] for k in track['keys']],
                         [list(zero), list(zero),
                          ['1', '0', '0', '1', '2', '0']])
        self.assert_worlds_match(source, delivered, 'prop',
                                 [0, Fraction(1, 2), 1, Fraction(3, 2), 2, 3])


class EncodingTests(WorldlockCase):
    """唯一解超出旧十进制编码要与代数无解区分。"""

    def request(self, detach):
        source = {
            'joints': [
                {'id': 'root', 'parent': None, 'local': list(IDENTITY)},
                {'id': 'prop', 'parent': 'root', 'local': ['1', '0', '0', '1', '1', '0']}],
            'tracks': [{'joint_id': 'root', 'keys': [
                {'tick': 0, 'matrix': ['3', '0', '0', '1', '1', '0']}]}],
            'vertices': [{'id': 'pv', 'influences': [
                {'joint_id': 'prop', 'point': ['0', '0'], 'weight': '1'}]}],
            'edges': [], 'sample_ticks': [0, 1]}
        return {'source': source,
                'edit': {'joint_id': 'root', 'keys': [
                    {'tick': 0, 'matrix': ['3', '0', '0', '1', '1', '0']},
                    {'tick': 1, 'matrix': ['3', '0', '0', '1', '3', '0']}]},
                'locks': [lock('prop', [0, 1], detach)]}

    def test_third_is_not_encodable(self):
        """唯一补偿须写入 1/3：不是无解，是超出原数值编码。"""
        req = self.write(self.request(detach=False))
        report = self.blocked_ok(req)
        self.assertEqual(report['blocked'], {'prop': {'kept': 'not_encodable'}})

    def test_same_request_detachable_compiles(self):
        """允许脱挂时同一请求可交付（脱挂补偿是常数世界矩阵）。"""
        req = self.write(self.request(detach=True))
        report, delivered, _ = self.compile_ok(req)
        self.assertEqual(report['locks'], {'prop': 'detached'})
        track = {t['joint_id']: t for t in delivered['tracks']}['prop']
        self.assertEqual([k['matrix'] for k in track['keys']],
                         [['3', '0', '0', '1', '4', '0'],
                          ['3', '0', '0', '1', '4', '0']])


class MixedLocksTests(WorldlockCase):
    """同一请求一个锁可解决、另一个被阻塞：不交付任何半成品。"""

    def test_no_partial_output(self):
        source = {
            'joints': [
                {'id': 'root', 'parent': None, 'local': list(IDENTITY)},
                {'id': 'prop_a', 'parent': 'root', 'local': ['1', '0', '0', '1', '2', '0']},
                {'id': 'mid', 'parent': 'root', 'local': ['3', '0', '0', '1', '0', '0']},
                {'id': 'prop_b', 'parent': 'mid', 'local': ['1', '0', '0', '1', '1', '0']}],
            'tracks': [{'joint_id': 'root', 'keys': [
                {'tick': 0, 'matrix': ['3', '0', '0', '1', '1', '0']}]}],
            'vertices': [{'id': 'pv', 'influences': [
                {'joint_id': 'prop_a', 'point': ['0', '0'], 'weight': '1'}]}],
            'edges': [], 'sample_ticks': [0, 1]}
        request = {'source': source,
                   'edit': {'joint_id': 'root', 'keys': [
                       {'tick': 0, 'matrix': ['3', '0', '0', '1', '1', '0']},
                       {'tick': 1, 'matrix': ['3', '0', '0', '1', '4', '0']}]},
                   'locks': [lock('prop_a', [0, 1], False),
                             lock('prop_b', [0, 1], False)]}
        req = self.write(request)
        report = self.blocked_ok(req)
        # prop_a 本可解决（补偿 x 平移 2→1），但 prop_b 须写 2/3
        self.assertEqual(report['blocked'], {'prop_b': {'kept': 'not_encodable'}})
        # prop_a 可解时不出现在阻塞清单里；整单不交付
        self.assertNotIn('prop_a', report['blocked'])

    def test_all_locks_solved_compiles_once(self):
        """两个锁都可解决时一次交付，各自按关节 ID 报形态。"""
        source = scenario1_source()
        source['joints'].append({'id': 'mid', 'parent': 'root', 'local': list(IDENTITY)})
        source['joints'].append({'id': 'prop_b', 'parent': 'mid',
                                 'local': ['1', '0', '0', '1', '0', '3']})
        request = {'source': source,
                   'edit': edit_root(['1', '0', '0', '1', '1', '0']),
                   'locks': [lock('prop', [0, 1], False),
                             lock('prop_b', [0, 1], True)]}
        req = self.write(request)
        report, delivered, _ = self.compile_ok(req)
        self.assertEqual(report['locks'], {'prop': 'kept', 'prop_b': 'kept'})
        self.assertEqual(report['options'],
                         {'prop': ['kept'], 'prop_b': ['kept', 'detached']})
        self.assert_worlds_match(source, delivered, 'prop', [Fraction(1, 2), 1, 7])
        self.assert_worlds_match(source, delivered, 'prop_b', [Fraction(1, 2), 1, 7])


class OwnTrackCutsTests(WorldlockCase):
    """锁定关节自己的原轨道键也参与切点：W_old 在补偿键之外的变化必须覆盖。"""

    def source(self):
        return {
            'joints': [
                {'id': 'root', 'parent': None, 'local': list(IDENTITY)},
                {'id': 'prop', 'parent': 'root', 'local': list(IDENTITY)}],
            'tracks': [{'joint_id': 'prop', 'keys': [
                {'tick': 0, 'matrix': list(IDENTITY)},
                {'tick': 5, 'matrix': ['1', '0', '0', '1', '5', '0']}]}],
            'vertices': [{'id': 'pv', 'influences': [
                {'joint_id': 'prop', 'point': ['0', '0'], 'weight': '1'}]}],
            'edges': [], 'sample_ticks': [0, 5]}

    def edit(self):
        return {'joint_id': 'root', 'keys': [
            {'tick': 0, 'matrix': list(IDENTITY)},
            {'tick': 1, 'matrix': ['1', '0', '0', '1', '1', '0']}]}

    def test_key_ticks_not_covering_own_track_motion_is_blocked(self):
        req = self.write({'source': self.source(), 'edit': self.edit(),
                          'locks': [lock('prop', [0, 1], False)]})
        report = self.blocked_ok(req)
        self.assertEqual(report['blocked'], {'prop': {'kept': 'no_exact_solution'}})

    def test_covering_key_ticks_compiles(self):
        req = self.write({'source': self.source(), 'edit': self.edit(),
                          'locks': [lock('prop', [0, 1, 5], False)]})
        report, delivered, _ = self.compile_ok(req)
        self.assertEqual(report['locks'], {'prop': 'kept'})
        track = {t['joint_id']: t for t in delivered['tracks']}['prop']
        self.assertEqual([k['matrix'] for k in track['keys']],
                         [list(IDENTITY), list(IDENTITY),
                          ['1', '0', '0', '1', '4', '0']])
        self.assert_worlds_match(self.source(), delivered, 'prop',
                                 [0, Fraction(1, 2), 1, 3, 5, 6, 1000000])


class RequestValidationTests(WorldlockCase):
    def base(self):
        return {'source': scenario1_source(),
                'edit': edit_root(['1', '0', '0', '1', '1', '0']),
                'locks': [lock('prop', [0, 1], False)]}

    def test_top_level_shape(self):
        self.bad_request(raw='[1,2]')
        self.bad_request(raw='"just a string"')
        body = self.base(); body['extra'] = 1
        self.bad_request(body)                       # 顶层多字段
        body = self.base(); del body['edit']
        self.bad_request(body)                       # 缺 edit
        body = self.base(); del body['locks']
        self.bad_request(body)                       # 缺 locks
        body = self.base(); del body['source']
        self.bad_request(body)                       # 缺 source

    def test_strict_json(self):
        self.bad_request(raw=b'\xff\xfe{')
        self.bad_request(raw='{"source":1,"source":2,"edit":{},"locks":[]}')
        body = json.dumps(self.base())
        self.bad_request(raw=body.replace('false', 'NaN'))

    def test_source_fully_validated(self):
        body = self.base()
        body['source']['vertices'][0]['influences'][0]['weight'] = '0.6'
        self.bad_request(body)                       # 权重和不为 1
        body = self.base()
        body['source']['sample_ticks'] = [1, 0]
        self.bad_request(body)                       # 采样表仍须合法

    def test_edit_track_validated(self):
        body = self.base(); body['edit']['joint_id'] = 'missing'
        self.bad_request(body)                       # 编辑未知关节
        body = self.base(); body['edit']['keys'] = []
        self.bad_request(body)                       # 空键表
        body = self.base()
        body['edit']['keys'][1] = {'tick': 0, 'matrix': list(IDENTITY)}
        self.bad_request(body)                       # 键时刻不递增
        body = self.base()
        body['edit']['keys'][1]['matrix'][4] = 1
        self.bad_request(body)                       # 矩阵系数须为旧格式字符串
        body = self.base(); body['edit']['note'] = 1
        self.bad_request(body)                       # 轨道多字段

    def test_lock_shape(self):
        body = self.base(); body['locks'] = []
        self.bad_request(body)                       # 至少 1 条
        body = self.base()
        body['locks'] = [lock(f'j{i}', [0, 1], False) for i in range(9)]
        self.bad_request(body)                       # 至多 8 条
        body = self.base(); body['locks'][0]['note'] = 1
        self.bad_request(body)                       # 锁记录多字段
        body = self.base(); body['locks'][0]['joint_id'] = 'missing'
        self.bad_request(body)                       # 未知锁定关节

    def test_key_ticks_rules(self):
        for ticks in ([], [0] * 2, [1, 0], [0, 0], [-1], [1000001], [0.5],
                      list(range(33))):
            body = self.base(); body['locks'][0]['key_ticks'] = ticks
            self.bad_request(body)
        body = self.base(); body['locks'][0]['key_ticks'] = list(range(32))
        code, out, _, _ = self.call(self.write(body))
        self.assertEqual(code, 0)                    # 32 个键合法

    def test_allow_detach_must_be_boolean(self):
        for value in ('true', 1, 0, None):
            body = self.base(); body['locks'][0]['allow_detach'] = value
            self.bad_request(body)

    def test_lock_must_be_strict_descendant_of_edit(self):
        body = self.base(); body['locks'][0]['joint_id'] = 'root'
        self.bad_request(body)                       # 锁定被编辑关节本身
        body = self.base(); body['edit']['joint_id'] = 'prop'
        self.bad_request(body)                       # 锁定非后代
        body = self.base()
        body['locks'] = [lock('tip', [0, 1], False), lock('tip', [0, 1], False)]
        self.bad_request(body)                       # 重复锁定
        body = self.base()
        body['locks'] = [lock('prop', [0, 1], False), lock('tip', [0, 1], False)]
        self.bad_request(body)                       # 互为祖先和后代

    def test_chain_depth_limit(self):
        joints = [{'id': 'j0', 'parent': None, 'local': list(IDENTITY)}]
        for i in range(1, 7):
            joints.append({'id': f'j{i}', 'parent': f'j{i - 1}',
                           'local': list(IDENTITY)})
        source = {'joints': joints, 'tracks': [],
                  'vertices': [{'id': 'v', 'influences': [
                      {'joint_id': 'j6', 'point': ['0', '0'], 'weight': '1'}]}],
                  'edges': [], 'sample_ticks': [0]}
        edit = {'joint_id': 'j0', 'keys': [
            {'tick': 0, 'matrix': list(IDENTITY)},
            {'tick': 1, 'matrix': ['1', '0', '0', '1', '1', '0']}]}
        body = {'source': source, 'edit': edit, 'locks': [lock('j6', [0, 1], False)]}
        self.bad_request(body)                       # 7 层：拒绝
        body['locks'] = [lock('j5', [0, 1], False)]
        code, out, _, _ = self.call(self.write(body))
        self.assertEqual(code, 0)                    # 6 层：接受

    def test_merged_ticks_limit(self):
        body = self.base()
        body['edit']['keys'] = [
            {'tick': k, 'matrix': ['1', '0', '0', '1', str(k), '0']}
            for k in range(78)]
        body['locks'][0]['key_ticks'] = [100]
        code, _, _, _ = self.call(self.write(body))
        self.assertEqual(code, 0)                    # 78+1+10^6 端点 = 80：接受
        body['locks'][0]['key_ticks'] = [100, 200]
        self.bad_request(body)                       # 81 个时刻：拒绝


class OutputAndCompatibilityTests(WorldlockCase):
    def request(self):
        return {'source': scenario1_source(),
                'edit': edit_root(['1', '0', '0', '1', '1', '0']),
                'locks': [lock('prop', [0, 1], False)]}

    def test_no_overwrite_and_symlink_safe(self):
        req = self.write(self.request())
        _, _, output = self.compile_ok(req)
        original = output.read_bytes()
        code, out, err, _ = self.call(req)
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('error:', err)
        self.assertEqual(output.read_bytes(), original)  # 已有目标不改写
        target = self.write(raw='{"keep": true}', name='target.json')
        link = self.root / 'link.json'
        os.symlink(target, link)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--request', str(req), '--output', str(link)])
        self.assertEqual(code, 2)
        self.assertEqual(target.read_text(encoding='utf-8'), '{"keep": true}')

    def test_blocked_and_bad_requests_leave_no_files(self):
        body = self.request()
        body['edit']['keys'][1]['matrix'] = ['2', '0', '0', '1', '1', '0']
        req = self.write(body)
        self.blocked_ok(req)
        before = sorted(p.name for p in self.root.iterdir())
        self.bad_request(raw='not json')  # 只新增请求文件本身
        after = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(len(after), len(before) + 1)
        self.assertNotIn('out.json', after)

    def test_creates_parent_directories(self):
        req = self.write(self.request())
        output = self.root / 'nested' / 'deep' / 'out.json'
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--request', str(req), '--output', str(output)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue(output.exists())

    def subprocess(self, module, *args):
        env = dict(os.environ)
        env['PYTHONPATH'] = PACKAGE_PARENT + os.pathsep + env.get('PYTHONPATH', '')
        return subprocess.run([sys.executable, '-m', module, *args],
                              capture_output=True, text=True, env=env)

    def test_module_entry_and_old_sampler_cli(self):
        req = self.write(self.request())
        output = self.root / 'sub-out.json'
        proc = self.subprocess('rig_pose.worldlock', '--request', str(req),
                               '--output', str(output))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['locks'], {'prop': 'kept'})
        # 交付件直接交给旧 rig-pose 命令采样
        report = self.root / 'pose-report.json'
        proc = self.subprocess('rig_pose.cli', '--source', str(output),
                               '--output', str(report))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary['sampled_frames'], 2)
        frames = json.loads(report.read_text(encoding='utf-8'))['frames']
        for f in frames:
            self.assertEqual(f['joint_world']['prop'], ['1', '0', '0', '1', '2', '0'])
        # 已存在目标退出 2 且不输出成功结果
        proc = self.subprocess('rig_pose.worldlock', '--request', str(req),
                               '--output', str(output))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, '')


if __name__ == '__main__':
    unittest.main()
