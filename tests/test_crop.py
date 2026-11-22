import contextlib
import hashlib
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
from rig_pose import poly
from rig_pose.crop import main, analyze, verify_proof

PACKAGE_PARENT = str(Path(rig_pose.__file__).resolve().parents[1])


def chain_source():
    """静止根 + 四级缩放链；顶点同时绑 c2、c4 与静止根（题目规定场景）。"""
    joints = [{'id': 'root', 'parent': None, 'local': ['1', '0', '0', '1', '0', '0']}]
    tracks = []
    for i in range(1, 5):
        parent = 'root' if i == 1 else f'c{i - 1}'
        joints.append({'id': f'c{i}', 'parent': parent, 'local': ['1', '0', '0', '1', '0', '0']})
        tracks.append({'joint_id': f'c{i}', 'keys': [
            {'tick': 0, 'matrix': ['0', '0', '0', '1', '0', '0']},
            {'tick': 1, 'matrix': ['1', '0', '0', '1', '0', '0']}]})
    return {
        'joints': joints,
        'tracks': tracks,
        'vertices': [{'id': 'v', 'influences': [
            {'joint_id': 'c2', 'point': ['-16', '0'], 'weight': '0.25'},
            {'joint_id': 'c4', 'point': ['16', '0'], 'weight': '0.25'},
            {'joint_id': 'root', 'point': ['2', '0'], 'weight': '0.5'}]}],
        'edges': [],
        'sample_ticks': [0, 1],
    }


def chain_request(left='0'):
    return {'source': chain_source(),
            'viewport': [
                {'tick': 0, 'min': [left, '-1'], 'max': ['2', '1']},
                {'tick': 1, 'min': [left, '-1'], 'max': ['2', '1']}]}


def static_source(x='0.5'):
    return {
        'joints': [{'id': 'root', 'parent': None, 'local': ['1', '0', '0', '1', '0', '0']}],
        'tracks': [],
        'vertices': [{'id': 'v', 'influences': [
            {'joint_id': 'root', 'point': [x, '0'], 'weight': '1'}]}],
        'edges': [],
        'sample_ticks': [0, 2],
    }


def hold_zero_source():
    """x 缩放 1→0→0→1：中段 [1,2] 上顶点 x 恒为 0（恒等贴边，且矩阵奇异）。"""
    return {
        'joints': [{'id': 'j', 'parent': None, 'local': ['1', '0', '0', '1', '0', '0']}],
        'tracks': [{'joint_id': 'j', 'keys': [
            {'tick': 0, 'matrix': ['1', '0', '0', '1', '0', '0']},
            {'tick': 1, 'matrix': ['0', '0', '0', '1', '0', '0']},
            {'tick': 2, 'matrix': ['0', '0', '0', '1', '0', '0']},
            {'tick': 3, 'matrix': ['1', '0', '0', '1', '0', '0']}]}],
        'vertices': [{'id': 'v', 'influences': [
            {'joint_id': 'j', 'point': ['1', '0'], 'weight': '1'}]}],
        'edges': [],
        'sample_ticks': [0, 3],
    }


def simple_request(source, viewport):
    return {'source': source, 'viewport': viewport}


def still_viewport(t0=0, t1=2, left='0', right='2', bottom='-1', top='1'):
    return [{'tick': t0, 'min': [left, bottom], 'max': [right, top]},
            {'tick': t1, 'min': [left, bottom], 'max': [right, top]}]


class CropCase(unittest.TestCase):
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

    def call(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def check(self, request_path, output_name='report.json'):
        output = self.root / output_name
        code, out, err = self.call(['check', '--request', str(request_path), '--output', str(output)])
        return code, out, err, output

    def verify(self, request_path, report_path):
        return self.call(['verify', '--request', str(request_path), '--report', str(report_path)])

    def ok_check(self, request_path, output_name='report.json'):
        code, out, err, output = self.check(request_path, output_name)
        self.assertEqual(code, 0, f'stderr: {err}')
        self.assertTrue(output.exists())
        return self.read(output), out, output

    def ok_verify(self, request_path, report_path, expect):
        code, out, err = self.verify(request_path, report_path)
        self.assertEqual(code, 0, f'stderr: {err}')
        self.assertEqual(out, '{"status":"%s"}\n' % expect)
        return out

    def bad_check(self, request_path, output_name=None):
        output_name = output_name or f'bad-output-{self.count}.json'
        code, out, err, output = self.check(request_path, output_name)
        self.assertEqual(code, 2, f'stdout: {out}')
        self.assertEqual(out, '')
        self.assertIn('error:', err)
        self.assertFalse(output.exists(), 'failed check must not create a report')
        return err

    def bad_verify(self, request_path, report_path):
        before = sorted(p.name for p in self.root.iterdir())
        code, out, err = self.verify(request_path, report_path)
        self.assertEqual(code, 2, f'stdout: {out}')
        self.assertEqual(out, '')
        self.assertIn('error:', err)
        after = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(before, after, 'failed verify must not leave new files')
        return err


class RequiredScenarioTests(CropCase):
    """任务书规定场景：四级缩放链顶点 x(t) = (2t^2-1)^2，中间恰贴左边。"""

    def test_safe_when_touching_left_edge(self):
        req = self.write(chain_request('0'))
        report, out, _ = self.ok_check(req)
        self.assertEqual(out, '{"status":"safe"}\n')
        self.assertEqual(report['status'], 'safe')
        self.assertEqual(report['request_sha256'],
                         hashlib.sha256(req.read_bytes()).hexdigest())
        self.assertEqual(report['credential']['version'], 1)
        self.assertEqual(report['credential']['kind'], 'safe')
        segments = report['credential']['segments']
        self.assertEqual([s['range'] for s in segments], [['0', '1']])
        proofs = {(c['vertex'], c['axis'], c['side']): c['proof']
                  for c in segments[0]['constraints']}
        self.assertEqual(set(proofs), {('v', a, s) for a in 'xy' for s in ('min', 'max')})
        # x(t) - 0 = (2t^2-1)^2 = 4*(t^2-1/2)^2：内部只接触不穿越
        self.assertEqual(proofs[('v', 'x', 'min')],
                         {'zero': False, 'odd': ['4'], 'square': ['-1/2', '0', '1'],
                          'sturm': [['4']], 'quotients': []})
        self.ok_verify(req, self.root / 'report.json', 'safe')

    def test_unsafe_when_left_edge_raised(self):
        req = self.write(chain_request('0.25'))
        report, out, _ = self.ok_check(req)
        self.assertEqual(out, '{"status":"unsafe"}\n')
        self.assertEqual(report['status'], 'unsafe')
        witness = report['credential']['witness']
        self.assertEqual(witness, {'vertex': 'v', 'axis': 'x', 'side': 'min',
                                   'tick': '3/4', 'coordinate': '1/64', 'boundary': '1/4'})
        self.ok_verify(req, self.root / 'report.json', 'unsafe')

    def unsafe_report(self):
        req = self.write(chain_request('0.25'))
        self.ok_check(req)
        return req, self.read(self.root / 'report.json')

    def tampered_verify(self, req, report):
        path = self.write(raw=json.dumps(report), name='tampered.json')
        return self.bad_verify(req, path)

    def test_witness_tick_replaced_by_endpoint_is_rejected(self):
        req, report = self.unsafe_report()
        for endpoint in ('0', '1'):
            forged = json.loads(json.dumps(report))
            forged['credential']['witness']['tick'] = endpoint
            self.tampered_verify(req, forged)

    def test_witness_coordinate_or_boundary_forged_is_rejected(self):
        req, report = self.unsafe_report()
        forged = json.loads(json.dumps(report))
        forged['credential']['witness']['coordinate'] = '1/63'
        self.tampered_verify(req, forged)
        forged = json.loads(json.dumps(report))
        forged['credential']['witness']['boundary'] = '1/2'
        self.tampered_verify(req, forged)

    def test_witness_other_tampering_is_rejected(self):
        req, report = self.unsafe_report()
        for key, value in (('vertex', 'nobody'), ('axis', 'y'), ('side', 'max'),
                           ('tick', '2'), ('tick', '1/2')):
            forged = json.loads(json.dumps(report))
            forged['credential']['witness'][key] = value
            self.tampered_verify(req, forged)
        # tick "1/2" 处坐标为 1/4 恰贴边，不是越界 → 拒绝
        forged = json.loads(json.dumps(report))
        forged['credential']['witness']['tick'] = '1/2'
        forged['credential']['witness']['coordinate'] = '1/4'
        self.tampered_verify(req, forged)
        # 另一个真实越界时刻（不要求最早）：t=5/8 时 x=49/1024 < 1/4 → 接受
        honest = json.loads(json.dumps(report))
        honest['credential']['witness'].update(
            {'tick': '5/8', 'coordinate': '49/1024', 'boundary': '1/4'})
        self.ok_verify(req, self.write(raw=json.dumps(honest), name='honest.json'), 'unsafe')


class TurningPointTests(CropCase):
    """安全框/轨道在采样表之外的转折必须参与判定。"""

    def test_viewport_turn_between_samples(self):
        source = static_source('0.5')
        viewport = [
            {'tick': 0, 'min': ['0', '-1'], 'max': ['2', '1']},
            {'tick': 1, 'min': ['0.9', '-1'], 'max': ['2', '1']},
            {'tick': 2, 'min': ['0', '-1'], 'max': ['2', '1']}]
        req = self.write(simple_request(source, viewport))
        report, _, _ = self.ok_check(req)
        self.assertEqual(report['status'], 'unsafe')
        self.assertEqual(report['credential']['witness'],
                         {'vertex': 'v', 'axis': 'x', 'side': 'min',
                          'tick': '1', 'coordinate': '1/2', 'boundary': '9/10'})
        self.ok_verify(req, self.root / 'report.json', 'unsafe')

    def test_track_turn_between_samples(self):
        source = static_source('0.5')
        source['tracks'] = [{'joint_id': 'root', 'keys': [
            {'tick': 0, 'matrix': ['1', '0', '0', '1', '0', '0']},
            {'tick': 1, 'matrix': ['1', '0', '0', '1', '-1', '0']},
            {'tick': 2, 'matrix': ['1', '0', '0', '1', '0', '0']}]}]
        req = self.write(simple_request(source, still_viewport()))
        report, _, _ = self.ok_check(req)
        self.assertEqual(report['status'], 'unsafe')
        self.assertEqual(report['credential']['witness'],
                         {'vertex': 'v', 'axis': 'x', 'side': 'min',
                          'tick': '1', 'coordinate': '-1/2', 'boundary': '0'})
        self.ok_verify(req, self.root / 'report.json', 'unsafe')

    def test_constant_on_edge_segment_is_safe(self):
        """整段恒等贴边（且姿态奇异）是合法镜头，证明含 zero 条目。"""
        req = self.write(simple_request(hold_zero_source(), still_viewport(0, 3)))
        report, out, _ = self.ok_check(req)
        self.assertEqual(report['status'], 'safe')
        segments = {tuple(s['range']): s for s in report['credential']['segments']}
        self.assertEqual(set(segments), {('0', '1'), ('1', '2'), ('2', '3')})
        middle = {(c['axis'], c['side']): c['proof'] for c in segments[('1', '2')]['constraints']}
        self.assertEqual(middle[('x', 'min')], {'zero': True})
        self.assertEqual(middle[('x', 'max')]['zero'], False)
        self.ok_verify(req, self.root / 'report.json', 'safe')

    def test_touch_exactly_at_interval_endpoint_is_safe(self):
        source = static_source('1')
        source['tracks'] = [{'joint_id': 'root', 'keys': [
            {'tick': 1, 'matrix': ['1', '0', '0', '1', '-1', '0']},
            {'tick': 2, 'matrix': ['1', '0', '0', '1', '0', '0']}]}]
        source['sample_ticks'] = [1, 2]
        req = self.write(simple_request(source, still_viewport(1, 2)))
        report, _, _ = self.ok_check(req)
        self.assertEqual(report['status'], 'safe')
        self.ok_verify(req, self.root / 'report.json', 'safe')


class SafeProofTamperingTests(CropCase):
    def setUp(self):
        super().setUp()
        self.req = self.write(chain_request('0'))
        self.report, _, _ = self.ok_check(self.req)
        self.report_path = self.root / 'report.json'

    def tampered(self, mutate):
        forged = json.loads(json.dumps(self.report))
        mutate(forged)
        path = self.write(raw=json.dumps(forged), name='forged.json')
        return self.bad_verify(self.req, path)

    def test_missing_one_coverage_item_is_rejected(self):
        def mutate(report):
            constraints = report['credential']['segments'][0]['constraints']
            report['credential']['segments'][0]['constraints'] = \
                [c for c in constraints if not (c['axis'] == 'y' and c['side'] == 'min')]
        err = self.tampered(mutate)
        self.assertIn('coverage', err)

    def test_missing_whole_segment_is_rejected(self):
        source = hold_zero_source()
        req = self.write(simple_request(source, still_viewport(0, 3)), name='req-hold.json')
        report, _, _ = self.ok_check(req, 'hold-report.json')
        report['credential']['segments'] = report['credential']['segments'][:2]
        path = self.write(raw=json.dumps(report), name='forged-hold.json')
        self.bad_verify(req, path)

    def test_transplanted_credential_with_updated_digest_is_rejected(self):
        """旧 safe 凭证搬到收紧后的安全框上，即使重填匹配的摘要也不通过。"""
        tightened = self.write(chain_request('0.25'), name='req-tight.json')
        forged = json.loads(json.dumps(self.report))
        forged['request_sha256'] = hashlib.sha256(tightened.read_bytes()).hexdigest()
        path = self.write(raw=json.dumps(forged), name='forged-tight.json')
        self.bad_verify(tightened, path)

    def test_wrong_request_digest_is_rejected(self):
        def mutate(report):
            report['request_sha256'] = '0' * 64
        self.tampered(mutate)

    def test_flipped_proof_coefficient_is_rejected(self):
        def mutate(report):
            for c in report['credential']['segments'][0]['constraints']:
                if (c['axis'], c['side']) == ('x', 'min'):
                    c['proof']['odd'] = ['5']
        self.tampered(mutate)

    def test_flipped_sturm_entry_is_rejected(self):
        def mutate(report):
            for c in report['credential']['segments'][0]['constraints']:
                if (c['axis'], c['side']) == ('x', 'max'):
                    c['proof']['sturm'][2] = ['-2', '0', '-2']
        self.tampered(mutate)

    def test_flipped_quotient_is_rejected(self):
        def mutate(report):
            for c in report['credential']['segments'][0]['constraints']:
                if (c['axis'], c['side']) == ('x', 'max'):
                    c['proof']['quotients'][0] = ['0', '1/2']
        self.tampered(mutate)

    def test_zero_flag_lies_are_rejected(self):
        def mutate_zero(report):
            for c in report['credential']['segments'][0]['constraints']:
                if (c['axis'], c['side']) == ('x', 'min'):
                    c['proof'] = {'zero': True}
        self.tampered(mutate_zero)

        def mutate_nonzero(report):
            for c in report['credential']['segments'][0]['constraints']:
                if (c['axis'], c['side']) == ('x', 'min'):
                    c['proof'] = {'zero': False, 'odd': ['2'], 'square': ['1'],
                                  'sturm': [['2']], 'quotients': []}
        self.tampered(mutate_nonzero)

    def test_unknown_or_missing_versions_are_rejected(self):
        for mutate in (
                lambda r: r.update(schema_version=2),
                lambda r: r.pop('schema_version'),
                lambda r: r['credential'].update(version=2),
                lambda r: r['credential'].pop('version'),
                lambda r: r['credential'].update(version='1')):
            self.tampered(mutate)

    def test_status_credential_mismatch_is_rejected(self):
        def mutate(report):
            report['credential']['kind'] = 'unsafe'
        self.tampered(mutate)

    def test_corrupted_credential_shapes_are_rejected(self):
        for mutate in (
                lambda r: r.update(credential=None),
                lambda r: r['credential'].pop('segments'),
                lambda r: r['credential'].update(extra=1),
                lambda r: r.update(tool='other'),
                lambda r: r.update(status='maybe'),
                lambda r: r.update(request_sha256='Z' * 64),
                lambda r: r['credential']['segments'][0].update(range=['0', '2']),
                lambda r: r['credential']['segments'][0]['constraints'][0].update(vertex='nobody')):
            self.tampered(mutate)

    def test_unreduced_or_malformed_rationals_are_rejected(self):
        for bad in ('2/4', '4/2', '0/5', '-0', '1.5', ' 1', '01', '1/0', '1/'):
            def mutate(report, bad=bad):
                for c in report['credential']['segments'][0]['constraints']:
                    if (c['axis'], c['side']) == ('x', 'min'):
                        c['proof']['square'] = [bad, '0', '1']
            self.tampered(mutate)


class RequestValidationTests(CropCase):
    def bad_request(self, body=None, raw=None):
        req = self.write(body, raw=raw)
        self.bad_check(req)

    def test_top_level_shape(self):
        self.bad_request(raw='[1,2]')
        self.bad_request(raw='"just a string"')
        self.bad_request({'viewport': still_viewport(0, 1)})          # 缺 source
        self.bad_request({'source': chain_source()})                  # 缺 viewport
        bad = chain_request('0')
        bad['extra'] = 1
        self.bad_request(bad)                                          # 顶层多字段

    def test_strict_json(self):
        self.bad_request(raw='{"source":1,"source":2,"viewport":[]}')
        self.bad_request(raw=b'\xff\xfe{')
        self.bad_request(raw=b'{"source":{},"viewport":[]}' + b' ' * 8000000)
        self.bad_request(raw='{"source":{"a":NaN},"viewport":[]}')

    def test_source_still_fully_validated(self):
        body = chain_request('0')
        body['source']['vertices'][0]['influences'][0]['weight'] = '0.6'
        self.bad_request(body)                                         # 权重和不为 1
        body = chain_request('0')
        body['source']['sample_ticks'] = [1, 0]
        self.bad_request(body)                                         # 采样表仍须合法

    def test_viewport_shape(self):
        source = chain_source()
        self.bad_request({'source': source, 'viewport': [
            {'tick': 0, 'min': ['0', '-1'], 'max': ['2', '1']}]})      # 只有 1 键
        self.bad_request({'source': source, 'viewport': [
            {'tick': i, 'min': ['0', '-1'], 'max': ['2', '1']} for i in range(22)]})  # 22 键
        self.bad_request({'source': source, 'viewport': [
            {'tick': 1, 'min': ['0', '-1'], 'max': ['2', '1']},
            {'tick': 0, 'min': ['0', '-1'], 'max': ['2', '1']}]})      # 不递增
        self.bad_request({'source': source, 'viewport': [
            {'tick': 0, 'min': ['0', '-1'], 'max': ['2', '1']},
            {'tick': 0, 'min': ['0', '-1'], 'max': ['2', '1']}]})      # 相等不递增
        for low, high in ((['2', '-1'], ['2', '1']), (['3', '-1'], ['2', '1']),
                          (['0', '1'], ['2', '1']), (['0', '5'], ['2', '1'])):
            self.bad_request({'source': source, 'viewport': [
                {'tick': 0, 'min': low, 'max': high},
                {'tick': 1, 'min': low, 'max': high}]})                # min 不严小于 max
        self.bad_request({'source': source, 'viewport': [
            {'tick': 0, 'min': ['0', '-1'], 'max': ['2', '1'], 'note': 'x'},
            {'tick': 1, 'min': ['0', '-1'], 'max': ['2', '1']}]})      # 键多字段
        self.bad_request({'source': source, 'viewport': [
            {'tick': 0, 'min': ['0'], 'max': ['2', '1']},
            {'tick': 1, 'min': ['0', '-1'], 'max': ['2', '1']}]})      # min 维数错
        self.bad_request({'source': source, 'viewport': [
            {'tick': 0, 'min': [0, '-1'], 'max': ['2', '1']},
            {'tick': 1, 'min': ['0', '-1'], 'max': ['2', '1']}]})      # 坐标须为旧格式字符串
        self.bad_request({'source': source, 'viewport': [
            {'tick': '0', 'min': ['0', '-1'], 'max': ['2', '1']},
            {'tick': 1, 'min': ['0', '-1'], 'max': ['2', '1']}]})      # tick 须为整数
        self.bad_request({'source': source, 'viewport': [
            {'tick': 0, 'min': ['0', '-1'], 'max': ['2', '1']},
            {'tick': 1000001, 'min': ['0', '-1'], 'max': ['2', '1']}]})  # tick 超域

    def test_tool_specific_limits(self):
        # 33 个顶点：拒绝
        source = chain_source()
        source['vertices'] = [{'id': f'v{i}', 'influences': [
            {'joint_id': 'root', 'point': ['1', '0'], 'weight': '1'}]} for i in range(33)]
        self.bad_request(simple_request(source, still_viewport(0, 1)))
        # 32 个顶点：接受
        source['vertices'] = source['vertices'][:32]
        req = self.write(simple_request(source, still_viewport(0, 1)))
        report, _, _ = self.ok_check(req, 'report-32.json')
        self.assertEqual(report['status'], 'safe')
        # 影响链 7 层：拒绝
        joints = [{'id': 'j0', 'parent': None, 'local': ['1', '0', '0', '1', '0', '0']}]
        for i in range(1, 7):
            joints.append({'id': f'j{i}', 'parent': f'j{i - 1}',
                           'local': ['1', '0', '0', '1', '0', '0']})
        deep = {'joints': joints, 'tracks': [],
                'vertices': [{'id': 'v', 'influences': [
                    {'joint_id': 'j6', 'point': ['1', '0'], 'weight': '1'}]}],
                'edges': [], 'sample_ticks': [0]}
        self.bad_request(simple_request(deep, still_viewport(0, 1)))
        # 影响链 6 层：接受
        deep['vertices'][0]['influences'][0]['joint_id'] = 'j5'
        req = self.write(simple_request(deep, still_viewport(0, 1)))
        report, _, _ = self.ok_check(req, 'report-6.json')
        self.assertEqual(report['status'], 'safe')

    def test_cut_point_limit(self):
        def keyed_source(keys):
            return {'joints': [{'id': 'root', 'parent': None,
                                'local': ['1', '0', '0', '1', '0', '0']}],
                    'tracks': [{'joint_id': 'root', 'keys': [
                        {'tick': k, 'matrix': ['1', '0', '0', '1', str(k), '0']}
                        for k in range(keys)]}],
                    'vertices': [{'id': 'v', 'influences': [
                        {'joint_id': 'root', 'point': ['0', '0'], 'weight': '1'}]}],
                    'edges': [], 'sample_ticks': [0]}
        # 21 个切点（轨道键 0..20 + 安全框首末）：接受
        req = self.write(simple_request(keyed_source(21), still_viewport(0, 20, right='25')))
        report, _, _ = self.ok_check(req)
        self.assertEqual(report['status'], 'safe')
        self.assertEqual(len(report['credential']['segments']), 20)
        # 22 个切点：拒绝
        self.bad_request(simple_request(keyed_source(22), still_viewport(0, 21, right='25')))


class OutputAndVerifyIoTests(CropCase):
    def test_check_does_not_overwrite_existing_file(self):
        req = self.write(chain_request('0'))
        report, _, output = self.ok_check(req)
        original = output.read_bytes()
        code, out, err, _ = self.check(req)
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('error:', err)
        self.assertEqual(output.read_bytes(), original)

    def test_check_does_not_follow_symlink(self):
        req = self.write(chain_request('0'))
        target = self.write(raw='{"keep": true}', name='target.json')
        link = self.root / 'link.json'
        os.symlink(target, link)
        code, out, err = self.call(['check', '--request', str(req), '--output', str(link)])
        self.assertEqual(code, 2)
        self.assertEqual(target.read_text(encoding='utf-8'), '{"keep": true}')

    def test_check_creates_parent_directories(self):
        req = self.write(chain_request('0'))
        output = self.root / 'nested' / 'deep' / 'report.json'
        code, out, err = self.call(['check', '--request', str(req), '--output', str(output)])
        self.assertEqual(code, 0, err)
        self.assertTrue(output.exists())

    def test_verify_missing_files(self):
        req = self.write(chain_request('0'))
        self.bad_verify(req, self.root / 'no-such-report.json')
        self.bad_verify(self.root / 'no-such-request.json', self.root / 'no-such-report.json')

    def test_verify_malformed_report(self):
        req = self.write(chain_request('0'))
        self.bad_verify(req, self.write(raw='not json', name='bad1.json'))
        self.bad_verify(req, self.write(raw='[1,2]', name='bad2.json'))
        self.bad_verify(req, self.write(raw='{"status":"safe"}', name='bad3.json'))
        self.bad_verify(req, self.write(raw=b'\xff\xfe', name='bad4.json'))

    def test_verify_rejects_invalid_request_even_with_matching_report(self):
        req = self.write(chain_request('0'))
        report, _, output = self.ok_check(req)
        # 请求被改坏（顶点 33 个），报告摘要已不匹配 → 拒绝
        body = chain_request('0')
        body['source']['vertices'] = [{'id': f'v{i}', 'influences': [
            {'joint_id': 'root', 'point': ['1', '0'], 'weight': '1'}]} for i in range(33)]
        bad_req = self.write(body, name='bad-req.json')
        self.bad_verify(bad_req, output)


class PolynomialUnitTests(unittest.TestCase):
    """直接检验 analyze 的精确判定与 verify_proof 的核验。"""

    def P(self, *coeffs):
        return tuple(Fraction(c) for c in coeffs)

    def assert_safe(self, p, a, b):
        proof, witness = analyze(p, a, b)
        self.assertIsNone(witness)
        verify_proof(p, a, b, proof)  # 不抛异常即通过
        return proof

    def assert_unsafe(self, p, a, b):
        proof, witness = analyze(p, a, b)
        self.assertIsNone(proof)
        self.assertLess(poly.evaluate(p, witness), 0)
        self.assertTrue(a <= witness <= b)
        return witness

    def test_zero_and_constant(self):
        self.assertEqual(self.assert_safe((), 0, 1), {'zero': True})
        self.assert_safe(self.P(5), 0, 1)
        self.assertEqual(self.assert_unsafe(self.P(-5), 0, 1), 0)

    def test_even_multiplicity_touch_is_safe(self):
        p = self.P(1, 0, -4, 0, 4)  # (2t^2-1)^2，内部恰贴 0
        proof = self.assert_safe(p, 0, 1)
        self.assertEqual(proof['odd'], ['4'])
        self.assert_safe(self.P(1, -2, 1), 0, 2)   # (t-1)^2
        self.assert_safe(self.P(1, 0, 0, 0, 0, 0, 1), 0, 1)  # t^6+1

    def test_sign_change_is_unsafe(self):
        witness = self.assert_unsafe(self.P(0, -2, 2), 0, 1)  # 2t(t-1) 在 (0,1) 为负
        self.assertGreater(witness, 0)
        self.assertLess(witness, 1)

    def test_endpoint_derivative_cases(self):
        self.assert_safe(self.P(-1, 1), 1, 2)    # t-1：左端贴边后上行
        self.assert_unsafe(self.P(1, -1), 1, 2)  # 1-t：左端贴边后下行
        self.assert_safe(self.P(1, -1), 0, 1)    # 1-t：右端贴边
        self.assert_unsafe(self.P(-1, 1), 0, 1)  # t-1：右端贴边但全程为负
        self.assert_unsafe(self.P(0, -1, 0, 1), 0, 2)   # t^3-t 在 (0,1) 为负
        self.assert_safe(self.P(0, -1, 0, 1), 1, 2)     # 同式在 [1,2] 非负

    def test_odd_multiplicity_root_inside(self):
        self.assert_unsafe(self.P(-1, 3, -3, 1), 0, 2)  # (t-1)^3 在 [0,1) 为负
        self.assert_safe(self.P(-1, 3, -3, 1), 1, 2)    # 同式在 [1,2] 非负

    def test_witness_uses_only_rationals(self):
        p = self.P(Fraction(3, 4), 0, -4, 0, 4)  # (2t^2-1)^2 - 1/4，只在子帧越界
        witness = self.assert_unsafe(p, 0, 1)
        self.assertIsInstance(witness, Fraction)
        self.assertNotEqual(witness.denominator, 1)

    def test_verify_proof_rejects_lies(self):
        p = self.P(1, 0, -4, 0, 4)
        proof, _ = analyze(p, 0, 1)
        forged = dict(proof, odd=['2'])
        with self.assertRaises(ValueError):
            verify_proof(p, 0, 1, forged)
        forged = dict(proof, zero=True)
        forged.pop('odd')
        with self.assertRaises(ValueError):
            verify_proof(p, 0, 1, forged)


class CliAndCompatibilityTests(CropCase):
    def subprocess(self, module, *args):
        env = dict(os.environ)
        env['PYTHONPATH'] = PACKAGE_PARENT + os.pathsep + env.get('PYTHONPATH', '')
        return subprocess.run([sys.executable, '-m', module, *args],
                              capture_output=True, text=True, env=env)

    def test_module_entry_points(self):
        req = self.write(chain_request('0'))
        output = self.root / 'sub-report.json'
        proc = self.subprocess('rig_pose.crop', 'check', '--request', str(req),
                               '--output', str(output))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), '{"status":"safe"}')
        proc = self.subprocess('rig_pose.crop', 'verify', '--request', str(req),
                               '--report', str(output))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), '{"status":"safe"}')
        proc = self.subprocess('rig_pose.crop', 'check', '--request', str(req),
                               '--output', str(output))
        self.assertEqual(proc.returncode, 2)  # 已存在不覆盖
        proc = self.subprocess('rig_pose.crop', 'verify', '--request', str(req),
                               '--report', str(req))
        self.assertEqual(proc.returncode, 2)  # 报告不是凭证
        self.assertEqual(proc.stdout, '')

    def test_original_pose_command_still_works(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'input.json'
        output = self.root / 'pose.json'
        proc = self.subprocess('rig_pose.cli', '--source', str(fixture), '--output', str(output))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary['sampled_frames'], 3)

    def test_spool_command_still_works(self):
        proc = self.subprocess('rig_pose.spool', '--spool', str(self.root / 'spool'),
                               '--request', self.write({'op': 'status'}, name='spool-req.json'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {'head': None, 'frames': []})


if __name__ == '__main__':
    unittest.main()
