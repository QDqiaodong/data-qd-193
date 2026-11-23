"""离线挂点世界锁定编译：rig-pose-worldlock --request FILE --output FILE。

动画师替换一条上游关节控制轨时，已摆好位置的挂点及其下方线稿要求在交付
动画里原地不动。本命令把这次控制轨替换与挂点补偿编译成一份普通的
rig-pose 源文件：每个锁定关节在编辑前的完整世界仿射矩阵，按旧关键帧
插值与端值保持规则，在整个闭区间 [0, 10^6] 的每个实数 tick 保持不变。

请求格式、交付形态、唯一可恢复判定与结果格式见 docs/worldlock.md。
全程精确有理数运算；不用浮点容差或量化误差满足锁定。
"""
import argparse
from bisect import bisect_right
from fractions import Fraction
import json
import sys

from . import poly
from .checks import array, fields, ident, integer, vector
from .crop import compose_polys, linear_poly
from .io import load, publish
from .model import validate

TOOL = 'rig-pose-worldlock'
MAX_LOCKS = 8        # 一次请求的锁条数上限
MAX_KEY_TICKS = 32   # 每条锁的补偿键个数上限
MAX_DEPTH = 6        # 锁定关节到根（含两端）的层数上限
MAX_CUTS = 80        # 链上源轨道键、替换键、补偿键与 0、10^6 合并去重后的时刻数上限
DECIMAL_SCALE = 1000000
DECIMAL_MAX = 999999999999999  # 旧有界十进制：整数最多 9 位、小数最多 6 位

KEPT = 'kept'
DETACHED = 'detached'
NO_SOLUTION = 'no_exact_solution'  # 全时域世界矩阵相等无精确解
NOT_UNIQUE = 'not_unique'          # 补偿不唯一
NOT_ENCODABLE = 'not_encodable'    # 唯一解超出原数值编码

CONST = -1  # 线性型中常数项的“变量”编号


# ---------------------------------------------------------- 线性型多项式
#
# 补偿轨的键值是未知量。区间 [a, b] 上补偿轨每个矩阵系数是 t 的一次式，
# 其系数又是未知键值的线性型。线性型 = {变量编号: 有理系数} 的字典，
# 常数项挂在 CONST 下；线性型多项式 = 按 t 的幂次排列的线性型元组。

def lp_const(p):
    """普通多项式提升为线性型多项式（每项只含常数）。"""
    return tuple({CONST: c} if c else {} for c in p)


def lp_var(index):
    return ({index: Fraction(1)},)


def lp_add(p, q):
    n = max(len(p), len(q))
    out = []
    for i in range(n):
        term = dict(p[i]) if i < len(p) else {}
        if i < len(q):
            for v, c in q[i].items():
                merged = term.get(v, 0) + c
                if merged:
                    term[v] = merged
                elif v in term:
                    del term[v]
        out.append(term)
    return tuple(out)


def lp_mul_plain(p, q):
    """线性型多项式 p 与普通（Fraction 系数）多项式 q 的卷积。"""
    if not p or not q:
        return ()
    out = [dict() for _ in range(len(p) + len(q) - 1)]
    for i, a in enumerate(p):
        if not a:
            continue
        for j, b in enumerate(q):
            if not b:
                continue
            term = out[i + j]
            for v, c in a.items():
                merged = term.get(v, 0) + c * b
                if merged:
                    term[v] = merged
                elif v in term:
                    del term[v]
    return tuple(out)


def compose_lin(parent, local):
    """affine.compose 的提升：parent 为普通多项式，local 为线性型多项式。"""
    a, b, c, d, x, y = parent
    e, f, g, h, u, v = local
    return (lp_add(lp_mul_plain(e, a), lp_mul_plain(f, c)),
            lp_add(lp_mul_plain(e, b), lp_mul_plain(f, d)),
            lp_add(lp_mul_plain(g, a), lp_mul_plain(h, c)),
            lp_add(lp_mul_plain(g, b), lp_mul_plain(h, d)),
            lp_add(lp_add(lp_mul_plain(u, a), lp_mul_plain(v, c)), lp_const(x)),
            lp_add(lp_add(lp_mul_plain(u, b), lp_mul_plain(v, d)), lp_const(y)))


# ---------------------------------------------------------- 精确线性系统

class LinearSystem:
    """稀疏有理线性系统：判定无解/唯一解/多解，唯一时回代求解。

    方程以 {变量: 系数}（含 CONST 常数项）表示“左端等于 0”。主元取最小
    变量编号，确定性强；新增方程先被既有主元行消元，再归一化入库，因此
    第 i 行只含更晚入库的主元，可倒序回代。
    """

    def __init__(self):
        self.rows = []        # (pivot_var, 归一化行)，pivot 系数为 1
        self.pivot_of = {}
        self.consistent = True

    def add(self, equation):
        if not self.consistent:
            return
        eq = {v: c for v, c in equation.items() if c}
        while True:
            variables = [v for v in eq if v != CONST]
            if not variables:
                if eq.get(CONST, 0) != 0:
                    self.consistent = False
                return
            pivot = min(variables)
            index = self.pivot_of.get(pivot)
            if index is None:
                inv = 1 / eq[pivot]
                row = {v: c * inv for v, c in eq.items()}
                self.pivot_of[pivot] = len(self.rows)
                self.rows.append((pivot, row))
                return
            _, row = self.rows[index]
            factor = eq[pivot]  # 主元行已归一：row[pivot] == 1
            reduced = dict(eq)
            for v, c in row.items():
                merged = reduced.get(v, 0) - factor * c
                if merged:
                    reduced[v] = merged
                elif v in reduced:
                    del reduced[v]
            eq = reduced

    def solve(self, nvars):
        """全部 nvars 个变量都被钉死时返回 {变量: 值}，否则 None（多解）。"""
        if any(v not in self.pivot_of for v in range(nvars)):
            return None
        solution = {}
        for pivot, row in reversed(self.rows):
            value = -row.get(CONST, 0)
            for v, c in row.items():
                if v == CONST or v == pivot:
                    continue
                value -= c * solution[v]
            solution[pivot] = value
        return solution


# ---------------------------------------------------------- 旧十进制编码

def encode_decimal(value):
    """把有理数编码为旧有界十进制字符串；不能精确表达时返回 None。"""
    scaled = value * DECIMAL_SCALE
    if scaled.denominator != 1:
        return None
    scaled = scaled.numerator
    if abs(scaled) > DECIMAL_MAX:
        return None
    sign = '-' if scaled < 0 else ''
    ip, fp = divmod(abs(scaled), DECIMAL_SCALE)
    if fp == 0:
        return f'{sign}{ip}'
    return f'{sign}{ip}.{fp:06d}'.rstrip('0')


# ---------------------------------------------------------- 请求模型

def parse_track(value, what):
    """一条原格式轨道：joint_id + 1..1000 个严格递增键。"""
    fields(value, ('joint_id', 'keys'))
    name = ident(value['joint_id'])
    keys = []
    previous = -1
    for key in array(value['keys'], 1, 1000):
        fields(key, ('tick', 'matrix'))
        tick = integer(key['tick'], 0, 1000000)
        if tick <= previous:
            raise ValueError(f'{what} key ticks strictly increase')
        previous = tick
        keys.append((tick, vector(key['matrix'], 6)))
    return name, keys


def parse_locks(value, joints):
    locks = []
    for item in array(value, 1, MAX_LOCKS):
        fields(item, ('joint_id', 'key_ticks', 'allow_detach'))
        name = ident(item['joint_id'])
        if name not in joints:
            raise ValueError(f'unknown lock joint: {name}')
        ticks = array(item['key_ticks'], 1, MAX_KEY_TICKS)
        previous = -1
        for tick in ticks:
            integer(tick, 0, 1000000)
            if tick <= previous:
                raise ValueError('lock key_ticks strictly increase')
            previous = tick
        detach = item['allow_detach']
        if type(detach) is not bool:
            raise ValueError('allow_detach must be a boolean')
        locks.append({'joint_id': name, 'key_ticks': list(ticks),
                      'allow_detach': detach})
    names = [lock['joint_id'] for lock in locks]
    if len(set(names)) != len(names):
        raise ValueError('duplicate lock joint')
    return locks


def chain_to_root(joints, name):
    """从关节自身到根的链（含两端）。"""
    chain = [name]
    while joints[chain[-1]]['parent'] is not None:
        chain.append(joints[chain[-1]]['parent'])
    return chain


def merged_cuts(tracks, edit_keys, chain, key_ticks):
    """链上源轨道键、替换轨道键、补偿键与 0、10^6 的合并去重时刻表。"""
    cuts = {0, 1000000}
    for name in chain:
        for tick, _ in tracks.get(name, ()):
            cuts.add(tick)
    for tick, _ in edit_keys:
        cuts.add(tick)
    cuts.update(key_ticks)
    cuts = sorted(cuts)
    if len(cuts) > MAX_CUTS:
        raise ValueError(f'more than {MAX_CUTS} merged ticks on a lock chain')
    return cuts


# ---------------------------------------------------------- 区间多项式

def local_polys(joints, tracks, name, a, b):
    """关节局部矩阵各系数在区间 [a, b] 上的多项式（端值保持 + 线性插值）。"""
    keys = tracks.get(name)
    if not keys:
        return tuple((c,) for c in joints[name]['local'])
    if b <= keys[0][0]:
        return tuple((c,) for c in keys[0][1])
    if a >= keys[-1][0]:
        return tuple((c,) for c in keys[-1][1])
    ticks = [t for t, _ in keys]
    i = bisect_right(ticks, a) - 1
    (t0, m0), (t1, m1) = keys[i], keys[i + 1]
    return tuple(linear_poly(c0, c1, t0, t1) for c0, c1 in zip(m0, m1))


def chain_world_polys(joints, tracks, chain_top_down, a, b):
    """沿自根向下的链组合世界矩阵多项式。"""
    world = None
    for name in chain_top_down:
        local = local_polys(joints, tracks, name, a, b)
        world = local if world is None else compose_polys(world, local)
    return world


def compensation_polys(key_ticks, a, b):
    """区间 [a, b] 上补偿轨六个系数的线性型多项式。

    未知量编号 var(s, k) = s*6 + k：第 s 个补偿键的第 k 个矩阵系数。
    区间必完整落在某个键段或端值保持区内（键时刻已并入切点）。
    """
    n = len(key_ticks)
    if b <= key_ticks[0]:
        return tuple(lp_var(k) for k in range(6))
    if a >= key_ticks[-1]:
        return tuple(lp_var((n - 1) * 6 + k) for k in range(6))
    s = bisect_right(key_ticks, a) - 1
    t0, t1 = key_ticks[s], key_ticks[s + 1]
    delta = t1 - t0
    alpha = (Fraction(-t0, delta), Fraction(1, delta))          # α(t)
    beta = (Fraction(1) + Fraction(t0, delta), Fraction(-1, delta))  # 1-α(t)
    out = []
    for k in range(6):
        lo, hi = s * 6 + k, (s + 1) * 6 + k
        terms = []
        for j in range(2):
            term = {}
            if beta[j]:
                term[lo] = beta[j]
            if alpha[j]:
                term[hi] = alpha[j]
            terms.append(term)
        out.append(tuple(terms))
    return tuple(out)


# ---------------------------------------------------------- 形态求解

def solve_form(joints, tracks_old, tracks_new, chain_top_down, key_ticks,
               form, cuts):
    """求解一种挂接形态。

    返回 ('ok', 每键六系数矩阵列表) 或 (失败原因,)。键矩阵系数先允许取
    任意有理数，以全时域世界矩阵相等为条件；恰有一解时再查旧十进制编码。
    """
    n = len(key_ticks)
    system = LinearSystem()
    for a, b in zip(cuts, cuts[1:]):
        world_old = chain_world_polys(joints, tracks_old, chain_top_down, a, b)
        if form == KEPT:
            parent_new = chain_world_polys(joints, tracks_new,
                                           chain_top_down[:-1], a, b)
        else:
            parent_new = tuple((Fraction(c),) for c in (1, 0, 0, 1, 0, 0))
        comp = compensation_polys(key_ticks, a, b)
        composed = compose_lin(parent_new, comp)
        for entry in range(6):
            diff = lp_add(composed[entry], lp_const(poly.neg(world_old[entry])))
            for term in diff:
                if term:
                    system.add(term)
        if not system.consistent:
            return (NO_SOLUTION,)
    solution = system.solve(n * 6)
    if solution is None:
        return (NOT_UNIQUE,)
    matrices = []
    for s in range(n):
        matrix = []
        for k in range(6):
            text = encode_decimal(solution[s * 6 + k])
            if text is None:
                return (NOT_ENCODABLE,)
            matrix.append(text)
        matrices.append(matrix)
    return ('ok', matrices)


# ---------------------------------------------------------- 编译

def compile_request(data):
    """校验并求解整个请求；返回 compiled（含交付源）或 blocked 结果。"""
    if not isinstance(data, dict):
        raise ValueError('request must be a JSON object')
    fields(data, ('source', 'edit', 'locks'))
    joints, order, tracks, vertices, edges, ticks = validate(data['source'])
    edit_name, edit_keys = parse_track(data['edit'], 'edit')
    if edit_name not in joints:
        raise ValueError(f'unknown edit joint: {edit_name}')
    locks = parse_locks(data['locks'], joints)
    chains = {}
    for lock in locks:
        chain = chain_to_root(joints, lock['joint_id'])
        if len(chain) > MAX_DEPTH:
            raise ValueError(
                f'lock chain of joint {lock["joint_id"]} exceeds {MAX_DEPTH} levels')
        if edit_name not in chain[1:]:
            raise ValueError(
                f'lock joint {lock["joint_id"]} is not a strict descendant '
                'of the edited joint')
        chains[lock['joint_id']] = chain
    for i, first in enumerate(locks):
        for second in locks[i + 1:]:
            if (first['joint_id'] in chains[second['joint_id']][1:]
                    or second['joint_id'] in chains[first['joint_id']][1:]):
                raise ValueError(
                    'lock joints must not be ancestor/descendant of each other')
    edited = dict(tracks)
    edited[edit_name] = edit_keys
    outcomes = {}
    blocked = {}
    for lock in locks:
        name = lock['joint_id']
        chain_top_down = list(reversed(chains[name]))
        cuts = merged_cuts(tracks, edit_keys, chains[name], lock['key_ticks'])
        attempts = {KEPT: solve_form(joints, tracks, edited, chain_top_down,
                                     lock['key_ticks'], KEPT, cuts)}
        if lock['allow_detach']:
            attempts[DETACHED] = solve_form(
                joints, tracks, edited, chain_top_down, lock['key_ticks'],
                DETACHED, cuts)
        options = [form for form in (KEPT, DETACHED)
                   if attempts.get(form, (None,))[0] == 'ok']
        # 确定的选择规则：保留挂接可交付时优先保留，否则才脱挂。
        if attempts[KEPT][0] == 'ok':
            chosen = KEPT
        elif attempts.get(DETACHED, (None,))[0] == 'ok':
            chosen = DETACHED
        else:
            blocked[name] = {form: result[0] for form, result in attempts.items()}
            continue
        outcomes[name] = {'form': chosen, 'options': options,
                          'matrices': attempts[chosen][1]}
    if blocked:
        return {'status': 'blocked', 'blocked': blocked}
    return {'status': 'compiled',
            'source': assemble(data, edit_name, locks, outcomes),
            'locks': {name: outcomes[name]['form'] for name in outcomes},
            'options': {name: outcomes[name]['options'] for name in outcomes}}


def assemble(data, edit_name, locks, outcomes):
    """拼装交付源：仅替换被编辑关节轨道与各锁定关节轨道，脱挂锁改 parent。"""
    source = data['source']
    detached = {name for name, outcome in outcomes.items()
                if outcome['form'] == DETACHED}
    out_joints = []
    for joint in source['joints']:
        entry = dict(joint)
        if entry['id'] in detached:
            entry['parent'] = None
        out_joints.append(entry)
    lock_tracks = {}
    for lock in locks:
        name = lock['joint_id']
        lock_tracks[name] = {'joint_id': name, 'keys': [
            {'tick': tick, 'matrix': matrix}
            for tick, matrix in zip(lock['key_ticks'],
                                    outcomes[name]['matrices'])]}
    edit_track = {'joint_id': edit_name, 'keys': data['edit']['keys']}
    out_tracks = []
    placed_edit = False
    placed_locks = set()
    for track in source['tracks']:
        name = track['joint_id']
        if name == edit_name:
            out_tracks.append(edit_track)
            placed_edit = True
        elif name in lock_tracks:
            out_tracks.append(lock_tracks[name])
            placed_locks.add(name)
        else:
            out_tracks.append(track)
    if not placed_edit:
        out_tracks.append(edit_track)
    for lock in locks:
        if lock['joint_id'] not in placed_locks:
            out_tracks.append(lock_tracks[lock['joint_id']])
    delivered = {'joints': out_joints, 'tracks': out_tracks,
                 'vertices': source['vertices'], 'edges': source['edges'],
                 'sample_ticks': source['sample_ticks']}
    # 交付件必须仍是合法的原版源（轨道总数等上限不被编辑与补偿撑破）。
    validate(delivered)
    return delivered


# ---------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(prog=TOOL)
    parser.add_argument('--request', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        outcome = compile_request(load(args.request))
        if outcome['status'] == 'compiled':
            publish(args.output, outcome['source'])
            report = {'status': 'compiled', 'locks': outcome['locks'],
                      'options': outcome['options']}
        else:
            report = {'status': 'blocked', 'blocked': outcome['blocked']}
    except (ValueError, OSError, UnicodeError, RecursionError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
