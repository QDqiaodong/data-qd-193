"""按帧交接 spool：单目录状态、原子提交、可重取响应。

rig-pose-spool --spool DIR --request FILE

一个目录只服务一份资产。业务状态（当前版本、当前帧记录、全部已记录
op 响应）保存在 DIR/state.json，通过 写临时文件 + fsync + os.replace
原子生效；DIR/lock 用 fcntl flock 序列化同一目录的所有请求。
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from .checks import fields, ident, integer
from .io import load
from .service import process

SCHEMA = 1
STATE_NAME = 'state.json'
LOCK_NAME = 'lock'
SHA256 = re.compile(r'[0-9a-f]{64}')


def initial_state():
    return {'schema_version': SCHEMA, 'head': None, 'frames': [], 'ops': {}}


def crashpoint(name):
    """测试钩子：在生效点前后模拟进程被终止（RIG_POSE_SPOOL_CRASH）。"""
    if os.environ.get('RIG_POSE_SPOOL_CRASH') == name:
        os._exit(137)


def parse_request(data):
    """校验请求形状与数据；revise 在此完整校验 source 并算出线稿。

    返回 (kind, op_id, args)。任何格式或数据错误抛 ValueError（退出 2），
    发生在读取/修改任何业务状态之前，因此失败不改变状态也不消耗 op_id。
    """
    if not isinstance(data, dict):
        raise ValueError('request must be a JSON object')
    op = data.get('op')
    if op == 'revise':
        fields(data, ('op', 'op_id', 'base', 'source'))
        op_id = ident(data['op_id'])
        base = data['base']
        if base is not None:
            ident(base)
        # source 是完整原版采样输入，沿用原字段语义、数量上限与管线。
        report = process(data['source'])
        art = {f['tick']: {'vertices': f['vertices'],
                           'edge_segments': f['edge_segments'],
                           'bbox': f['bbox']} for f in report['frames']}
        return 'revise', op_id, {'base': base, 'art': art}
    if op == 'claim':
        fields(data, ('op', 'op_id', 'tick'))
        return 'claim', ident(data['op_id']), {'tick': integer(data['tick'], 0, 1000000)}
    if op == 'complete':
        fields(data, ('op', 'op_id', 'claim_id', 'artifact_sha256'))
        sha = data['artifact_sha256']
        if not isinstance(sha, str) or not SHA256.fullmatch(sha):
            raise ValueError('artifact_sha256 must be 64 lowercase hex characters')
        return 'complete', ident(data['op_id']), {'claim_id': ident(data['claim_id']), 'sha256': sha}
    if op == 'status':
        fields(data, ('op',))
        return 'status', None, {}
    raise ValueError(f'unknown op: {op!r}')


def load_state(spool):
    path = Path(spool) / STATE_NAME
    if not path.exists():
        return initial_state()
    with open(path, 'rb') as f:
        state = json.loads(f.read().decode('utf-8'))
    if not isinstance(state, dict) or state.get('schema_version') != SCHEMA:
        raise ValueError('unsupported spool state schema')
    return state


def commit_state(spool, state):
    """业务变化与对应可重取响应在同一原子 rename 中生效。"""
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    fd, temporary = tempfile.mkstemp(dir=spool, prefix='.state-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        crashpoint('before_commit')
        os.replace(temporary, Path(spool) / STATE_NAME)
        dirfd = os.open(spool, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        crashpoint('after_commit')
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


class locked:
    """同一目录内所有请求（含 status）在 flock 下串行。"""

    def __init__(self, spool):
        self.spool = spool
        self.fd = None

    def __enter__(self):
        self.fd = os.open(Path(self.spool) / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o666)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
        return False


def apply_revise(state, op_id, args):
    if args['base'] != state['head']:
        return {'status': 'base_conflict', 'head': state['head']}
    previous = {f['tick']: f for f in state['frames']}
    art = args['art']
    kept, opened, removed = [], [], []
    frames = []
    for tick in sorted(art):
        old = previous.get(tick)
        if old is not None and old['payload'] == art[tick]:
            kept.append(tick)
            frames.append(old)  # 完整保留：状态、已发凭据、已收产物
        else:
            opened.append(tick)
            frames.append({'tick': tick, 'epoch': op_id, 'state': 'pending',
                           'claim_id': None, 'artifact_sha256': None,
                           'payload': art[tick]})
    for tick in sorted(previous):
        if tick not in art:
            removed.append(tick)
    state['head'] = op_id
    state['frames'] = frames
    return {'status': 'revised', 'head': op_id,
            'kept': kept, 'opened': opened, 'removed': removed}


def apply_claim(state, op_id, args):
    for frame in state['frames']:
        if frame['tick'] == args['tick']:
            if frame['state'] != 'pending':
                return {'status': 'not_claimable'}
            frame['state'] = 'claimed'
            frame['claim_id'] = op_id
            return {'status': 'claimed', 'claim_id': op_id, 'tick': frame['tick'],
                    'epoch': frame['epoch'], 'payload': frame['payload']}
    return {'status': 'not_claimable'}


def apply_complete(state, op_id, args):
    for frame in state['frames']:
        if frame['state'] == 'claimed' and frame['claim_id'] == args['claim_id']:
            frame['state'] = 'ready'
            frame['artifact_sha256'] = args['sha256']
            return {'status': 'accepted', 'tick': frame['tick'], 'epoch': frame['epoch']}
    return {'status': 'stale_claim'}


def status_view(state):
    frames = sorted(state['frames'], key=lambda f: f['tick'])
    return {'head': state['head'], 'frames': [
        {'tick': f['tick'], 'epoch': f['epoch'], 'state': f['state'],
         'claim_id': f['claim_id'], 'artifact_sha256': f['artifact_sha256']}
        for f in frames]}


def handle(spool, request):
    """处理一个已解析请求，返回响应对象；错误抛异常由调用方转退出 2。"""
    # 解析后整个原请求的规范形：忽略键顺序与空白，数组顺序与字符串内容有意义。
    canonical = json.dumps(request, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False)
    kind, op_id, args = parse_request(request)
    with locked(spool):
        for stale in Path(spool).glob('.state-*'):  # 清理生效前终止留下的临时文件
            try:
                stale.unlink()
            except OSError:
                pass
        state = load_state(spool)
        if kind == 'status':
            return status_view(state)
        record = state['ops'].get(op_id)
        if record is not None:
            if record['request'] != canonical:
                raise ValueError(f'op_id {op_id} reused with a different request')
            return record['response']  # 返回原响应，不重新判断，也不再写业务状态
        if kind == 'revise':
            response = apply_revise(state, op_id, args)
        elif kind == 'claim':
            response = apply_claim(state, op_id, args)
        else:
            response = apply_complete(state, op_id, args)
        state['ops'][op_id] = {'request': canonical, 'response': response}
        commit_state(spool, state)
        return response


def main(argv=None):
    parser = argparse.ArgumentParser(prog='rig-pose-spool')
    parser.add_argument('--spool', required=True)
    parser.add_argument('--request', required=True)
    args = parser.parse_args(argv)
    try:
        request = load(args.request)
        spool = Path(args.spool)
        spool.mkdir(parents=True, exist_ok=True)
        response = handle(str(spool), request)
    except (ValueError, OSError, UnicodeError, RecursionError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    print(json.dumps(response, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
