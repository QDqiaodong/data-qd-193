"""离线安全框验收：rig-pose-crop check / verify。

合成组在任意子帧叠加画面，采样表之外的运动也必须落在随时间收放的
矩形安全框内。动画方用 check 对整个闭合验收区间做精确判定并产出带
纯数据凭证的 JSON 报告；合成方用 verify 只凭原请求与报告独立核验
凭证（精确有理数运算），不重跑检查、不搜寻违例、不补造证明。

请求、报告、见证与 safe 证明的格式及核验规则见 docs/crop.md。
"""
import argparse
import hashlib
import json
import math
import re
import sys
from bisect import bisect_right
from fractions import Fraction

from .affine import compose, point
from .animate import sample_track
from .checks import array, fields, integer, vector
from .io import constant, pairs, publish
from .model import validate
from . import poly

TOOL = 'rig-pose-crop'
SCHEMA_VERSION = 1
CREDENTIAL_VERSION = 1
REQUEST_LIMIT = 8000000
REPORT_LIMIT = 1000000000
MAX_VERTICES = 32
MAX_DEPTH = 6
MAX_CUTS = 21
MAX_POLY_LEN = 7   # constraint polynomials have degree <= 6
MAX_CHAIN_LEN = 7  # sturm chain of a degree <= 6 polynomial
SIDES = (('x', 'min'), ('x', 'max'), ('y', 'min'), ('y', 'max'))
AXIS_INDEX = {'x': 0, 'y': 1}
SHA256_RE = re.compile(r'[0-9a-f]{64}')
RATIONAL_RE = re.compile(r'-?(?:0|[1-9][0-9]*)(?:/([1-9][0-9]*))?\Z')


# ---------------------------------------------------------------- 请求读取

def read_raw(path, limit):
    with open(path, 'rb') as f:
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f'input over {limit} bytes')
    return raw


def parse_strict(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=constant)


def parse_rational(value):
    """约分后的整数/分数字符串："3"、"-2"、"3/4"；分母为正、分子分母互素。"""
    if not isinstance(value, str):
        raise ValueError('rational must be a string')
    match = RATIONAL_RE.fullmatch(value)
    if not match or value == '-0':
        raise ValueError(f'invalid rational string: {value!r}')
    if match.group(1) is None:
        return Fraction(value)
    num, den = value.split('/')
    n, d = int(num), int(den)
    if d == 1 or math.gcd(abs(n), d) != 1:
        raise ValueError(f'fraction not in lowest terms: {value!r}')
    return Fraction(n, d)


def parse_poly(value, max_len=MAX_POLY_LEN):
    if not isinstance(value, list) or len(value) > max_len:
        raise ValueError('polynomial must be an array within the degree bound')
    coeffs = tuple(parse_rational(x) for x in value)
    if coeffs and coeffs[-1] == 0:
        raise ValueError('polynomial not normalized (trailing zero)')
    return coeffs


def encode_poly(p):
    return [str(c) for c in p]


# ---------------------------------------------------------------- 请求模型

def parse_viewport(value):
    keys = []
    previous = -1
    for item in array(value, 2, 21):
        fields(item, ('tick', 'min', 'max'))
        tick = integer(item['tick'], 0, 1000000)
        if tick <= previous:
            raise ValueError('viewport ticks strictly increase')
        previous = tick
        low = vector(item['min'], 2)
        high = vector(item['max'], 2)
        if not (low[0] < high[0] and low[1] < high[1]):
            raise ValueError('viewport min must be strictly below max on each axis')
        keys.append((tick, low, high))
    return keys


def linear_poly(c0, c1, t0, t1):
    """Polynomial in t interpolating c0 at t0 to c1 at t1 (t0 != t1)."""
    beta = Fraction(c1 - c0, t1 - t0)
    return poly.norm((c0 - beta * t0, beta))


def local_polys(model, name, a, b):
    """Local matrix coefficients of one joint as polynomials on segment [a, b]."""
    keys = model.tracks.get(name)
    if not keys:
        return tuple((c,) for c in model.joints[name]['local'])
    if b <= keys[0][0]:
        return tuple((c,) for c in keys[0][1])
    if a >= keys[-1][0]:
        return tuple((c,) for c in keys[-1][1])
    ticks = [t for t, _ in keys]
    i = bisect_right(ticks, a) - 1
    (t0, m0), (t1, m1) = keys[i], keys[i + 1]
    return tuple(linear_poly(c0, c1, t0, t1) for c0, c1 in zip(m0, m1))


def compose_polys(parent, local):
    """Same composition order as affine.compose, lifted to polynomials."""
    a, b, c, d, x, y = parent
    e, f, g, h, u, v = local
    return (poly.add(poly.mul(a, e), poly.mul(c, f)),
            poly.add(poly.mul(b, e), poly.mul(d, f)),
            poly.add(poly.mul(a, g), poly.mul(c, h)),
            poly.add(poly.mul(b, g), poly.mul(d, h)),
            poly.add(poly.add(poly.mul(a, u), poly.mul(c, v)), x),
            poly.add(poly.add(poly.mul(b, u), poly.mul(d, v)), y))


def edge_polys(viewport, a, b):
    """The four viewport edges (left, right, bottom, top) as linear polynomials."""
    ticks = [k[0] for k in viewport]
    i = bisect_right(ticks, a) - 1
    (t0, mn0, mx0), (t1, mn1, mx1) = viewport[i], viewport[i + 1]
    return (linear_poly(mn0[0], mn1[0], t0, t1),
            linear_poly(mx0[0], mx1[0], t0, t1),
            linear_poly(mn0[1], mn1[1], t0, t1),
            linear_poly(mx0[1], mx1[1], t0, t1))


class SegmentData:
    """Exact polynomial expressions shared by all constraints of one segment."""

    def __init__(self, model, a, b):
        world = {}
        for name in model.order:
            if name not in model.chain:
                continue
            local = local_polys(model, name, a, b)
            parent = model.joints[name]['parent']
            world[name] = compose_polys(world[parent], local) if parent is not None else local
        self.positions = {}
        for vid, influences in model.vertices.items():
            x = poly.ZERO
            y = poly.ZERO
            for joint, (px, py), w in influences:
                A, B, C, D, TX, TY = world[joint]
                term_x = poly.add(poly.add(poly.mul(A, (px,)), poly.mul(C, (py,))), TX)
                term_y = poly.add(poly.add(poly.mul(B, (px,)), poly.mul(D, (py,))), TY)
                x = poly.add(x, poly.scale(term_x, w))
                y = poly.add(y, poly.scale(term_y, w))
            self.positions[vid] = (x, y)
        self.edges = edge_polys(model.viewport, a, b)

    def constraint(self, vid, axis, side):
        """p(t) >= 0 on the segment expresses this vertex staying inside one edge."""
        x, y = self.positions[vid]
        left, right, bottom, top = self.edges
        if (axis, side) == ('x', 'min'):
            return poly.sub(x, left)
        if (axis, side) == ('x', 'max'):
            return poly.sub(right, x)
        if (axis, side) == ('y', 'min'):
            return poly.sub(y, bottom)
        return poly.sub(top, y)


class Model:
    """Validated crop request: motion, viewport and the cut decomposition."""

    def __init__(self, data):
        fields(data, ('source', 'viewport'))
        joints, order, tracks, vertices, edges, ticks = validate(data['source'])
        if len(vertices) > MAX_VERTICES:
            raise ValueError(f'crop accepts at most {MAX_VERTICES} vertices')
        viewport = parse_viewport(data['viewport'])
        self.joints, self.order, self.tracks, self.vertices = joints, order, tracks, vertices
        self.viewport = viewport
        self.t0 = viewport[0][0]
        self.t1 = viewport[-1][0]
        influenced = {j for influences in vertices.values() for j, _, _ in influences}
        depth = {}

        def depth_of(name):
            if name not in depth:
                parent = joints[name]['parent']
                depth[name] = 1 if parent is None else depth_of(parent) + 1
            return depth[name]

        for name in influenced:
            if depth_of(name) > MAX_DEPTH:
                raise ValueError(f'influence chain of joint {name} exceeds {MAX_DEPTH} levels')
        chain = set()
        for name in influenced:
            while name is not None:
                chain.add(name)
                name = joints[name]['parent']
        self.chain = chain
        cuts = {tick for tick, _, _ in viewport}
        for name in chain:
            for tick, _ in tracks.get(name, ()):
                if self.t0 <= tick <= self.t1:
                    cuts.add(tick)
        cuts = sorted(cuts)
        if len(cuts) > MAX_CUTS:
            raise ValueError(f'more than {MAX_CUTS} cut points in acceptance interval')
        self.cuts = cuts
        self.segments = list(zip(cuts, cuts[1:]))
        self._segments = {}

    def segment_data(self, a, b):
        if (a, b) not in self._segments:
            self._segments[(a, b)] = SegmentData(self, a, b)
        return self._segments[(a, b)]

    def position_at(self, vid, t):
        """Deformed vertex position at any rational t in the acceptance interval."""
        memo = {}

        def world(name):
            if name not in memo:
                joint = self.joints[name]
                local = sample_track(self.tracks[name], t) if name in self.tracks else joint['local']
                parent = joint['parent']
                memo[name] = compose(world(parent), local) if parent is not None else local
            return memo[name]

        x = Fraction(0)
        y = Fraction(0)
        for joint, xy, w in self.vertices[vid]:
            wx, wy = point(world(joint), xy)
            x += w * wx
            y += w * wy
        return x, y

    def edge_at(self, axis, side, t):
        """Viewport edge value at any rational t in the acceptance interval."""
        ticks = [k[0] for k in self.viewport]
        i = bisect_right(ticks, t) - 1
        index = AXIS_INDEX[axis]
        if i >= len(self.viewport) - 1:
            low, high = self.viewport[-1][1], self.viewport[-1][2]
            return (low if side == 'min' else high)[index]
        (t0, mn0, mx0), (t1, mn1, mx1) = self.viewport[i], self.viewport[i + 1]
        v0 = (mn0 if side == 'min' else mx0)[index]
        v1 = (mn1 if side == 'min' else mx1)[index]
        return v0 + (v1 - v0) * Fraction(t - t0, t1 - t0)


# ---------------------------------------------------------------- check

def analyze(p, a, b):
    """Decide "p(t) >= 0 for all t in [a, b]" exactly.

    Returns (proof_entry, None) when safe, (None, witness_t) when unsafe.
    """
    if not p:
        return {'zero': True}, None
    odd, square = poly.odd_part(p)
    chain, quotients = poly.sturm_chain(odd)
    va = poly.evaluate(odd, a)
    vb = poly.evaluate(odd, b)
    d_odd = poly.deriv(odd)
    unsafe = (va < 0 or vb < 0
              or (va == 0 and poly.evaluate(d_odd, a) <= 0)
              or (vb == 0 and poly.evaluate(d_odd, b) >= 0)
              or poly.roots_open(chain, a, b) != 0)
    if unsafe:
        return None, poly.negative_witness(p, odd, chain, a, b)
    proof = {'zero': False,
             'odd': encode_poly(odd),
             'square': encode_poly(square),
             'sturm': [encode_poly(s) for s in chain],
             'quotients': [encode_poly(q) for q in quotients]}
    return proof, None


def check(data, request_sha256):
    """Run the acceptance check and return the report object (not yet written)."""
    model = Model(data)
    segments = []
    for a, b in model.segments:
        seg = model.segment_data(a, b)
        constraints = []
        for vid in sorted(model.vertices):
            for axis, side in SIDES:
                proof, witness = analyze(seg.constraint(vid, axis, side), a, b)
                if witness is not None:
                    return unsafe_report(model, vid, axis, side, witness, request_sha256)
                constraints.append({'vertex': vid, 'axis': axis, 'side': side, 'proof': proof})
        segments.append({'range': [str(a), str(b)], 'constraints': constraints})
    return {'schema_version': SCHEMA_VERSION, 'tool': TOOL, 'status': 'safe',
            'request_sha256': request_sha256,
            'credential': {'version': CREDENTIAL_VERSION, 'kind': 'safe',
                           'segments': segments}}


def unsafe_report(model, vid, axis, side, t, request_sha256):
    coordinate = model.position_at(vid, t)[AXIS_INDEX[axis]]
    boundary = model.edge_at(axis, side, t)
    witness = {'vertex': vid, 'axis': axis, 'side': side,
               'tick': str(t), 'coordinate': str(coordinate), 'boundary': str(boundary)}
    return {'schema_version': SCHEMA_VERSION, 'tool': TOOL, 'status': 'unsafe',
            'request_sha256': request_sha256,
            'credential': {'version': CREDENTIAL_VERSION, 'kind': 'unsafe',
                           'witness': witness}}


# ---------------------------------------------------------------- verify

def verify(raw_request, raw_report):
    """Independently verify a report against the original request bytes.

    Returns 'safe' or 'unsafe' when the credential checks out; raises
    ValueError (exit 2) for any request, report or credential problem.
    """
    report = parse_strict(raw_report)
    if not isinstance(report, dict):
        raise ValueError('report must be a JSON object')
    fields(report, ('schema_version', 'tool', 'status', 'request_sha256', 'credential'))
    if type(report['schema_version']) is not int or report['schema_version'] != SCHEMA_VERSION:
        raise ValueError('unknown report schema version')
    if report['tool'] != TOOL:
        raise ValueError('unexpected report tool marker')
    status = report['status']
    if status not in ('safe', 'unsafe'):
        raise ValueError('unknown report status')
    digest = report['request_sha256']
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ValueError('malformed request_sha256')
    if digest != hashlib.sha256(raw_request).hexdigest():
        raise ValueError('report does not match this request (request_sha256)')
    model = Model(parse_strict(raw_request))
    credential = report['credential']
    if not isinstance(credential, dict):
        raise ValueError('credential must be an object')
    if type(credential.get('version')) is not int or credential['version'] != CREDENTIAL_VERSION:
        raise ValueError('unknown credential version')
    if credential.get('kind') != status:
        raise ValueError('credential kind does not match report status')
    if status == 'unsafe':
        fields(credential, ('version', 'kind', 'witness'))
        verify_witness(model, credential['witness'])
    else:
        fields(credential, ('version', 'kind', 'segments'))
        verify_safe(model, credential['segments'])
    return status


def verify_witness(model, witness):
    fields(witness, ('vertex', 'axis', 'side', 'tick', 'coordinate', 'boundary'))
    vid = witness['vertex']
    if not isinstance(vid, str) or vid not in model.vertices:
        raise ValueError('witness references an unknown vertex')
    axis = witness['axis']
    if axis not in AXIS_INDEX:
        raise ValueError('witness axis must be x or y')
    side = witness['side']
    if side not in ('min', 'max'):
        raise ValueError('witness side must be min or max')
    t = parse_rational(witness['tick'])
    if not model.t0 <= t <= model.t1:
        raise ValueError('witness tick outside the acceptance interval')
    coordinate = parse_rational(witness['coordinate'])
    boundary = parse_rational(witness['boundary'])
    actual = model.position_at(vid, t)[AXIS_INDEX[axis]]
    if coordinate != actual:
        raise ValueError('witness coordinate does not match the motion at its tick')
    edge = model.edge_at(axis, side, t)
    if boundary != edge:
        raise ValueError('witness boundary does not match the viewport at its tick')
    violated = coordinate < boundary if side == 'min' else coordinate > boundary
    if not violated:
        raise ValueError('witness shows no strict violation')


def verify_safe(model, segments):
    if not isinstance(segments, list) or len(segments) != len(model.segments):
        raise ValueError('credential segments do not match the request cut decomposition')
    required = {(vid, axis, side) for vid in model.vertices for axis, side in SIDES}
    for entry, (a, b) in zip(segments, model.segments):
        fields(entry, ('range', 'constraints'))
        rng = entry['range']
        if not (isinstance(rng, list) and len(rng) == 2
                and rng[0] == str(a) and rng[1] == str(b)):
            raise ValueError('credential segment range does not match the request cuts')
        constraints = entry['constraints']
        if not isinstance(constraints, list) or len(constraints) != len(required):
            raise ValueError('credential constraint coverage does not match the request')
        seen = set()
        seg = model.segment_data(a, b)
        for item in constraints:
            fields(item, ('vertex', 'axis', 'side', 'proof'))
            key = (item['vertex'], item['axis'], item['side'])
            if key not in required or key in seen:
                raise ValueError('credential constraint coverage does not match the request')
            seen.add(key)
            verify_proof(seg.constraint(*key), a, b, item['proof'])


def verify_proof(p, a, b, proof):
    """Check one exact non-negativity proof for constraint p on [a, b]."""
    if not isinstance(proof, dict) or 'zero' not in proof:
        raise ValueError('malformed proof entry')
    if proof['zero'] is True:
        fields(proof, ('zero',))
        if p:
            raise ValueError('zero proof given for a nonzero constraint polynomial')
        return
    fields(proof, ('zero', 'odd', 'square', 'sturm', 'quotients'))
    if proof['zero'] is not False:
        raise ValueError('proof zero flag must be true or false')
    if not p:
        raise ValueError('nonzero proof given for an identically zero constraint')
    odd = parse_poly(proof['odd'])
    square = parse_poly(proof['square'])
    if not odd or not square:
        raise ValueError('odd and square factors must be nonzero')
    if poly.mul(odd, poly.mul(square, square)) != p:
        raise ValueError('odd*square^2 does not equal the constraint polynomial')
    chain_value = proof['sturm']
    if not isinstance(chain_value, list) or not 1 <= len(chain_value) <= MAX_CHAIN_LEN:
        raise ValueError('sturm chain length out of bounds')
    chain = [parse_poly(s) for s in chain_value]
    quotients_value = proof['quotients']
    if not isinstance(quotients_value, list) or len(quotients_value) != len(chain) - 1:
        raise ValueError('sturm quotient count does not match the chain')
    quotients = [parse_poly(q) for q in quotients_value]
    if chain[0] != odd:
        raise ValueError('sturm chain does not start at the odd factor')
    if len(chain) == 1:
        if len(odd) != 1:
            raise ValueError('single-entry sturm chain requires a constant odd factor')
    elif chain[1] != poly.deriv(odd):
        raise ValueError('sturm chain second entry must be the odd factor derivative')
    if len(chain[-1]) != 1:
        raise ValueError('sturm chain must end in a nonzero constant')
    for i in range(1, len(chain)):
        nxt = chain[i + 1] if i + 1 < len(chain) else poly.ZERO
        if poly.sub(poly.mul(quotients[i - 1], chain[i]), nxt) != chain[i - 1]:
            raise ValueError('sturm chain quotient/remainder relation does not hold')
    if poly.roots_open(chain, a, b) != 0:
        raise ValueError('sturm count shows a sign-change root inside the segment')
    va = poly.evaluate(odd, a)
    vb = poly.evaluate(odd, b)
    if va < 0 or vb < 0:
        raise ValueError('constraint is negative at a segment endpoint')
    d_odd = poly.deriv(odd)
    if va == 0 and poly.evaluate(d_odd, a) <= 0:
        raise ValueError('constraint goes negative right of the segment start')
    if vb == 0 and poly.evaluate(d_odd, b) >= 0:
        raise ValueError('constraint goes negative left of the segment end')


# ---------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(prog='rig-pose-crop')
    sub = parser.add_subparsers(dest='command', required=True)
    p_check = sub.add_parser('check', help='produce an acceptance report with credential')
    p_check.add_argument('--request', required=True)
    p_check.add_argument('--output', required=True)
    p_verify = sub.add_parser('verify', help='independently verify a report against the request')
    p_verify.add_argument('--request', required=True)
    p_verify.add_argument('--report', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'check':
            raw = read_raw(args.request, REQUEST_LIMIT)
            report = check(parse_strict(raw), hashlib.sha256(raw).hexdigest())
            publish(args.output, report)
            status = report['status']
        else:
            raw_request = read_raw(args.request, REQUEST_LIMIT)
            raw_report = read_raw(args.report, REPORT_LIMIT)
            status = verify(raw_request, raw_report)
        print(json.dumps({'status': status}, separators=(',', ':')))
        return 0
    except (ValueError, OSError, UnicodeError, RecursionError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
