import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import rig_pose
from rig_pose.service import process
from rig_pose.spool import main

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'input.json'
PACKAGE_PARENT = str(Path(rig_pose.__file__).resolve().parents[1])
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64
SHA_D = 'd' * 64
SHA_E = 'e' * 64


def source():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def revised_source():
    """只为 root 插入 tick 5、平移 x=6 的关键帧（其他系数同原轨道）。"""
    data = source()
    data['tracks'][0]['keys'].insert(1, {'tick': 5, 'matrix': ['1', '0', '0', '1', '6', '0']})
    return data


class SpoolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spool = self.root / 'spool'
        self.spool.mkdir()
        self.reqdir = self.root / 'requests'
        self.reqdir.mkdir()
        self.count = 0

    def request_file(self, body, raw=None):
        self.count += 1
        path = self.reqdir / f'req-{self.count}.json'
        if raw is None:
            path.write_text(json.dumps(body, ensure_ascii=False), encoding='utf-8')
        elif isinstance(raw, bytes):
            path.write_bytes(raw)
        else:
            path.write_text(raw, encoding='utf-8')
        return path

    def call(self, body=None, raw=None, env_extra=None, subprocess_mode=False):
        path = self.request_file(body, raw)
        if subprocess_mode or env_extra:
            env = dict(os.environ)
            env['PYTHONPATH'] = PACKAGE_PARENT + os.pathsep + env.get('PYTHONPATH', '')
            env.update(env_extra or {})
            proc = subprocess.run(
                [sys.executable, '-m', 'rig_pose.spool',
                 '--spool', str(self.spool), '--request', str(path)],
                capture_output=True, text=True, env=env)
            return proc.returncode, proc.stdout, proc.stderr
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--spool', str(self.spool), '--request', str(path)])
        return code, out.getvalue(), err.getvalue()

    def popen(self, body):
        path = self.request_file(body)
        env = dict(os.environ)
        env['PYTHONPATH'] = PACKAGE_PARENT + os.pathsep + env.get('PYTHONPATH', '')
        return subprocess.Popen(
            [sys.executable, '-m', 'rig_pose.spool',
             '--spool', str(self.spool), '--request', str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    def ok(self, body=None, raw=None):
        code, out, err = self.call(body, raw=raw)
        self.assertEqual(code, 0, f'stderr: {err}')
        return json.loads(out)

    def bad(self, body=None, raw=None):
        code, out, err = self.call(body, raw=raw)
        self.assertEqual(code, 2, f'stdout: {out}')
        self.assertEqual(out, '')
        self.assertIn('error:', err)
        return err

    def revise(self, op_id, base, src):
        return self.ok({'op': 'revise', 'op_id': op_id, 'base': base, 'source': src})

    def claim(self, op_id, tick):
        return self.ok({'op': 'claim', 'op_id': op_id, 'tick': tick})

    def complete(self, op_id, claim_id, sha):
        return self.ok({'op': 'complete', 'op_id': op_id,
                        'claim_id': claim_id, 'artifact_sha256': sha})

    def status(self):
        return self.ok({'op': 'status'})

    def frame(self, st, tick):
        for f in st['frames']:
            if f['tick'] == tick:
                return f
        return None

    def test_handoff_chain_revision_retention_and_replay(self):
        r = self.revise('r1', None, source())
        self.assertEqual(r, {'status': 'revised', 'head': 'r1',
                             'kept': [], 'opened': [0, 5, 10], 'removed': []})

        c0 = self.claim('c0', 0)
        self.assertEqual(set(c0), {'status', 'claim_id', 'tick', 'epoch', 'payload'})
        self.assertEqual(c0['status'], 'claimed')
        self.assertEqual(c0['claim_id'], 'c0')
        self.assertEqual(c0['epoch'], 'r1')
        self.assertEqual(set(c0['payload']), {'vertices', 'edge_segments', 'bbox'})
        self.assertEqual(c0['payload']['vertices'], {'blend': ['1', '0'], 'tip': ['3', '0']})
        d0 = self.complete('done0', 'c0', SHA_A)
        self.assertEqual(d0, {'status': 'accepted', 'tick': 0, 'epoch': 'r1'})

        c5 = self.claim('c5', 5)
        self.assertEqual(c5['payload']['vertices']['blend'], ['6', '0'])
        c10 = self.claim('c10', 10)
        self.assertEqual(c10['status'], 'claimed')

        r2 = self.revise('r2', 'r1', revised_source())
        self.assertEqual(r2, {'status': 'revised', 'head': 'r2',
                              'kept': [0, 10], 'opened': [5], 'removed': []})

        st = self.status()
        self.assertEqual(st['head'], 'r2')
        self.assertEqual([f['tick'] for f in st['frames']], [0, 5, 10])
        f0, f5, f10 = st['frames']
        self.assertEqual(f0, {'tick': 0, 'epoch': 'r1', 'state': 'ready',
                              'claim_id': 'c0', 'artifact_sha256': SHA_A})
        self.assertEqual(f5, {'tick': 5, 'epoch': 'r2', 'state': 'pending',
                              'claim_id': None, 'artifact_sha256': None})
        self.assertEqual(f10, {'tick': 10, 'epoch': 'r1', 'state': 'claimed',
                               'claim_id': 'c10', 'artifact_sha256': None})

        # 保留帧上的旧领取现在仍可交付
        d10 = self.complete('done10', 'c10', SHA_B)
        self.assertEqual(d10, {'status': 'accepted', 'tick': 10, 'epoch': 'r1'})
        # tick 5 已开新世代，旧凭据失效
        self.assertEqual(self.complete('done5late', 'c5', SHA_C), {'status': 'stale_claim'})

        c5b = self.claim('c5b', 5)
        self.assertEqual(c5b['epoch'], 'r2')
        self.assertEqual(c5b['payload']['vertices']['blend'], ['7', '0'])

        # 还原原输入：tick 5 线稿回到 A，但退休世代不恢复，再开新世代
        r3 = self.revise('r3', 'r2', source())
        self.assertEqual(r3, {'status': 'revised', 'head': 'r3',
                              'kept': [0, 10], 'opened': [5], 'removed': []})
        st = self.status()
        self.assertEqual(self.frame(st, 5), {'tick': 5, 'epoch': 'r3', 'state': 'pending',
                                             'claim_id': None, 'artifact_sha256': None})
        self.assertEqual(self.frame(st, 0)['artifact_sha256'], SHA_A)
        self.assertEqual(self.frame(st, 10)['artifact_sha256'], SHA_B)
        # 上一世代的领取凭据交付仍为 stale_claim
        self.assertEqual(self.complete('done5b', 'c5b', SHA_D), {'status': 'stale_claim'})
        # 用原领取请求重试：取得同一份旧线稿（不重新判断可领取性）
        again = self.claim('c5', 5)
        self.assertEqual(again, c5)
        self.assertEqual(again['payload']['vertices']['blend'], ['6', '0'])
        # 新世代可用新 op_id 正常领取
        c5c = self.claim('c5c', 5)
        self.assertEqual(c5c['status'], 'claimed')
        self.assertEqual(c5c['epoch'], 'r3')
        self.assertEqual(c5c['payload']['vertices']['blend'], ['6', '0'])

    def test_delete_then_readd_opens_fresh_epoch(self):
        self.revise('r1', None, source())
        self.claim('c5', 5)
        self.complete('done5', 'c5', SHA_A)
        dropped = source()
        dropped['sample_ticks'] = [0, 10]
        r2 = self.revise('r2', 'r1', dropped)
        self.assertEqual(r2, {'status': 'revised', 'head': 'r2',
                              'kept': [0, 10], 'opened': [], 'removed': [5]})
        self.assertIsNone(self.frame(self.status(), 5))
        r3 = self.revise('r3', 'r2', source())
        self.assertEqual(r3['opened'], [5])
        self.assertEqual(r3['removed'], [])
        f5 = self.frame(self.status(), 5)
        self.assertEqual(f5, {'tick': 5, 'epoch': 'r3', 'state': 'pending',
                              'claim_id': None, 'artifact_sha256': None})
        self.assertEqual(self.complete('done5late', 'c5', SHA_B), {'status': 'stale_claim'})

    def test_source_revision_without_line_art_change_keeps_frames(self):
        self.revise('r1', None, source())
        self.claim('c5', 5)
        same = source()
        # 插入的关键帧值等于原插值结果：源变了，采样线稿不变
        same['tracks'][0]['keys'].insert(1, {'tick': 5, 'matrix': ['1', '0', '0', '1', '5', '0']})
        r2 = self.revise('r2', 'r1', same)
        self.assertEqual(r2, {'status': 'revised', 'head': 'r2',
                              'kept': [0, 5, 10], 'opened': [], 'removed': []})
        f5 = self.frame(self.status(), 5)
        self.assertEqual(f5['state'], 'claimed')
        self.assertEqual(f5['claim_id'], 'c5')
        self.assertEqual(f5['epoch'], 'r1')
        self.assertEqual(self.complete('done5', 'c5', SHA_E),
                         {'status': 'accepted', 'tick': 5, 'epoch': 'r1'})

    def test_edge_topology_change_is_line_art_change(self):
        self.revise('r1', None, source())
        self.claim('c0', 0)
        self.complete('done0', 'c0', SHA_A)
        noedges = source()
        noedges['edges'] = []
        r2 = self.revise('r2', 'r1', noedges)
        self.assertEqual(r2, {'status': 'revised', 'head': 'r2',
                              'kept': [], 'opened': [0, 5, 10], 'removed': []})
        for f in self.status()['frames']:
            self.assertEqual(f['state'], 'pending')
            self.assertEqual(f['epoch'], 'r2')
            self.assertIsNone(f['claim_id'])
            self.assertIsNone(f['artifact_sha256'])

    def test_base_conflict_response_is_recorded_and_replayed(self):
        self.revise('r1', None, source())
        self.revise('r2', 'r1', revised_source())
        conflict = self.revise('r3', 'r1', source())
        self.assertEqual(conflict, {'status': 'base_conflict', 'head': 'r2'})
        self.revise('r4', 'r2', source())
        # 重放给当时的 head，不重新判断 base，也不回退当前版本
        self.assertEqual(self.revise('r3', 'r1', source()),
                         {'status': 'base_conflict', 'head': 'r2'})
        self.assertEqual(self.status()['head'], 'r4')

    def test_first_revise_requires_null_base(self):
        r = self.revise('r1', 'elsewhere', source())
        self.assertEqual(r, {'status': 'base_conflict', 'head': None})
        self.assertEqual(self.revise('r1', 'elsewhere', source()), r)
        self.assertEqual(self.revise('r2', None, source())['status'], 'revised')

    def test_not_claimable_cases(self):
        self.assertEqual(self.claim('cX', 0), {'status': 'not_claimable'})
        self.revise('r1', None, source())
        self.assertEqual(self.claim('c99', 99), {'status': 'not_claimable'})
        self.claim('c5', 5)
        self.assertEqual(self.claim('c5again', 5), {'status': 'not_claimable'})

    def test_stale_claim_cases(self):
        self.revise('r1', None, source())
        self.assertEqual(self.complete('d1', 'nobody', SHA_A), {'status': 'stale_claim'})
        self.claim('c5', 5)
        self.complete('d2', 'c5', SHA_B)
        # 已 ready 的帧凭据不再是 claimed
        self.assertEqual(self.complete('d3', 'c5', SHA_C), {'status': 'stale_claim'})

    def test_op_id_reuse_with_different_request_is_input_error(self):
        self.revise('r1', None, source())
        self.bad({'op': 'revise', 'op_id': 'r1', 'base': 'r1', 'source': revised_source()})
        self.assertEqual(self.status()['head'], 'r1')
        self.assertEqual(self.revise('r1', None, source())['status'], 'revised')
        # 所有写操作共享一个 op_id 命名空间
        self.bad({'op': 'claim', 'op_id': 'r1', 'tick': 5})
        self.claim('c5', 5)
        self.bad({'op': 'claim', 'op_id': 'c5', 'tick': 0})
        self.assertEqual(self.frame(self.status(), 0)['state'], 'pending')
        self.assertEqual(self.claim('c5', 5)['status'], 'claimed')

    def test_request_format_errors(self):
        self.bad(raw='{"op":"status","op":"status"}')
        self.bad(raw='{"op":"claim","op_id":"x","tick":NaN}')
        self.bad(raw='{"op":"claim","op_id":"x","tick":1e999}')
        self.bad(raw='[1,2,3]')
        self.bad(raw='"just a string"')
        self.bad(raw=b'\xff\xfe{')
        self.bad(raw=b'{"op":"status"}' + b' ' * 8000000)
        self.bad(raw='{"op":"bogus"}')
        self.bad(raw='{"op":"status","extra":1}')
        self.bad(raw='{}')
        self.bad({'op': 'claim', 'op_id': 'c1'})
        self.bad({'op': 'claim', 'op_id': 'c1', 'tick': -1})
        self.bad({'op': 'claim', 'op_id': 'c1', 'tick': 1000001})
        self.bad({'op': 'claim', 'op_id': 'c1', 'tick': '5'})
        self.bad({'op': 'claim', 'op_id': 'c1', 'tick': 5, 'extra': 1})
        self.bad({'op': 'claim', 'op_id': 'bad id', 'tick': 5})
        self.bad({'op': 'claim', 'op_id': 'x' * 65, 'tick': 5})
        self.bad({'op': 'complete', 'op_id': 'd1', 'claim_id': 'c1', 'artifact_sha256': 'A' * 64})
        self.bad({'op': 'complete', 'op_id': 'd1', 'claim_id': 'c1', 'artifact_sha256': 'a' * 63})
        self.bad({'op': 'complete', 'op_id': 'd1', 'claim_id': 'c1', 'artifact_sha256': 'g' * 64})
        self.bad({'op': 'revise', 'op_id': 'r1', 'base': None})
        self.bad({'op': 'revise', 'op_id': 'r1', 'base': 7, 'source': source()})
        # 失败请求不消耗 ID、不改变状态
        self.assertEqual(self.claim('c1', 5), {'status': 'not_claimable'})
        self.assertEqual(self.status(), {'head': None, 'frames': []})

    def test_invalid_revise_neither_consumes_id_nor_touches_frames(self):
        self.revise('r1', None, source())
        self.claim('c0', 0)
        self.complete('done0', 'c0', SHA_A)
        self.claim('c5', 5)
        broken = source()
        broken['vertices'][1]['influences'][0]['weight'] = '0.6'
        self.bad({'op': 'revise', 'op_id': 'r2', 'base': 'r1', 'source': broken})
        # 版本已落后但 source 无效：数据错误（退出 2），不是 base_conflict
        self.bad({'op': 'revise', 'op_id': 'rX', 'base': 'elsewhere', 'source': broken})
        st = self.status()
        self.assertEqual(st['head'], 'r1')
        self.assertEqual(self.frame(st, 0)['state'], 'ready')
        self.assertEqual(self.frame(st, 0)['artifact_sha256'], SHA_A)
        self.assertEqual(self.frame(st, 5)['state'], 'claimed')
        self.assertEqual(self.frame(st, 5)['claim_id'], 'c5')
        # 失败未消耗 ID：修正内容后同 ID 可正常发布
        r = self.revise('r2', 'r1', revised_source())
        self.assertEqual(r['status'], 'revised')

    def test_crash_before_commit_leaves_no_trace(self):
        self.revise('r1', None, source())
        self.claim('c5', 5)
        body = {'op': 'revise', 'op_id': 'r2', 'base': 'r1', 'source': revised_source()}
        code, out, _ = self.call(body, env_extra={'RIG_POSE_SPOOL_CRASH': 'before_commit'})
        self.assertEqual(code, 137)
        self.assertEqual(out, '')
        st = self.status()
        self.assertEqual(st['head'], 'r1')
        self.assertEqual(self.frame(st, 5)['state'], 'claimed')
        self.assertEqual(self.frame(st, 5)['claim_id'], 'c5')
        # ID 未被消耗：同请求重试即新鲜处理
        r = self.revise('r2', 'r1', revised_source())
        self.assertEqual(r, {'status': 'revised', 'head': 'r2',
                             'kept': [0, 10], 'opened': [5], 'removed': []})

    def test_crash_after_commit_is_retrievable(self):
        self.revise('r1', None, source())
        body = {'op': 'revise', 'op_id': 'r2', 'base': 'r1', 'source': revised_source()}
        code, out, _ = self.call(body, env_extra={'RIG_POSE_SPOOL_CRASH': 'after_commit'})
        self.assertEqual(code, 137)
        self.assertEqual(out, '')
        # 新进程可见已生效状态
        st = self.status()
        self.assertEqual(st['head'], 'r2')
        self.assertEqual(self.frame(st, 5)['epoch'], 'r2')
        expected = {'status': 'revised', 'head': 'r2',
                    'kept': [0, 10], 'opened': [5], 'removed': []}
        self.assertEqual(self.revise('r2', 'r1', revised_source()), expected)
        # 另一请求发布下一版后，重试中断修订仍给当时的 head 与分类，且不回退当前版本
        self.revise('r3', 'r2', source())
        self.assertEqual(self.revise('r2', 'r1', revised_source()), expected)
        self.assertEqual(self.status()['head'], 'r3')

    def test_concurrent_revises_exactly_one_wins(self):
        self.revise('r1', None, source())
        noedges = source()
        noedges['edges'] = []
        bodies = {'wA': {'op': 'revise', 'op_id': 'wA', 'base': 'r1', 'source': revised_source()},
                  'wB': {'op': 'revise', 'op_id': 'wB', 'base': 'r1', 'source': noedges}}
        procs = {k: self.popen(b) for k, b in bodies.items()}
        outcomes = {}
        for key, proc in procs.items():
            out, err = proc.communicate()
            self.assertEqual(proc.returncode, 0, err)
            outcomes[key] = json.loads(out)
        winners = [k for k, v in outcomes.items() if v['status'] == 'revised']
        self.assertEqual(len(winners), 1, outcomes)
        winner = winners[0]
        loser = 'wB' if winner == 'wA' else 'wA'
        self.assertEqual(outcomes[loser], {'status': 'base_conflict', 'head': winner})
        st = self.status()
        self.assertEqual(st['head'], winner)
        if winner == 'wA':
            self.assertEqual(self.frame(st, 5)['epoch'], 'wA')
            self.assertEqual(self.frame(st, 0)['epoch'], 'r1')
        else:
            self.assertEqual(self.frame(st, 0)['epoch'], 'wB')
        # 状态与重试响应都与实际胜负一致
        for key, body in bodies.items():
            self.assertEqual(self.revise(key, 'r1', body['source']), outcomes[key])

    def test_empty_spool_status_and_ops(self):
        self.assertEqual(self.status(), {'head': None, 'frames': []})
        self.assertEqual(self.claim('c1', 0), {'status': 'not_claimable'})
        self.assertEqual(self.complete('d1', 'c1', SHA_A), {'status': 'stale_claim'})

    def test_missing_spool_directory_is_created_and_shared_across_processes(self):
        self.spool = self.root / 'fresh' / 'nested'
        self.assertEqual(self.status(), {'head': None, 'frames': []})
        self.assertEqual(self.revise('r1', None, source())['status'], 'revised')
        self.claim('c5', 5)
        # 新进程打开同一目录接续既有状态
        code, out, err = self.call({'op': 'status'}, subprocess_mode=True)
        self.assertEqual(code, 0, err)
        st = json.loads(out)
        self.assertEqual(st['head'], 'r1')
        self.assertEqual(self.frame(st, 5)['state'], 'claimed')

    def test_claim_payload_matches_rig_pose_report(self):
        src = source()
        self.revise('r1', None, src)
        claimed = self.claim('c5', 5)
        frame = process(src)['frames'][1]
        self.assertEqual(claimed['payload'], {'vertices': frame['vertices'],
                                              'edge_segments': frame['edge_segments'],
                                              'bbox': frame['bbox']})


if __name__ == '__main__':
    unittest.main()
