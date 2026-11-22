"""Exact univariate rational polynomials for rig-pose-crop.

A polynomial is a tuple of fractions.Fraction, lowest degree first, with no
trailing zeros; the zero polynomial is the empty tuple. Every operation is
exact rational arithmetic — no floats, no tolerances.

The safety decision of the crop tool reduces to "p(t) >= 0 for all t in
[a, b]" for rational polynomials of degree <= 6. This module provides the
exact machinery for that decision and for locating a rational violating
point: Yun square-free factorization (p = odd * square**2 with odd
square-free), Sturm chains, and Sturm-based sign-change location.
"""
from fractions import Fraction

ZERO = ()
ONE = (Fraction(1),)


def norm(p):
    n = len(p)
    while n and p[n - 1] == 0:
        n -= 1
    return tuple(p[:n])


def add(p, q):
    n = max(len(p), len(q))
    return norm(tuple((p[i] if i < len(p) else 0) + (q[i] if i < len(q) else 0)
                      for i in range(n)))


def neg(p):
    return tuple(-c for c in p)


def sub(p, q):
    return add(p, neg(q))


def scale(p, f):
    if not p or f == 0:
        return ZERO
    return tuple(c * f for c in p)


def mul(p, q):
    if not p or not q:
        return ZERO
    out = [Fraction(0)] * (len(p) + len(q) - 1)
    for i, a in enumerate(p):
        if a:
            for j, b in enumerate(q):
                if b:
                    out[i + j] += a * b
    return norm(tuple(out))


def deriv(p):
    return tuple(p[i] * i for i in range(1, len(p)))


def evaluate(p, x):
    acc = Fraction(0)
    for c in reversed(p):
        acc = acc * x + c
    return acc


def divmod_exact(num, den):
    """Polynomial division over Q: (quotient, remainder), deg(rem) < deg(den)."""
    if not den:
        raise ValueError('polynomial division by zero')
    if len(num) < len(den):
        return ZERO, num
    rem = list(num)
    quo = [Fraction(0)] * (len(num) - len(den) + 1)
    inv = Fraction(1) / den[-1]
    for k in range(len(quo) - 1, -1, -1):
        c = rem[k + len(den) - 1] * inv
        quo[k] = c
        if c:
            for j in range(len(den)):
                rem[k + j] -= c * den[j]
    return norm(tuple(quo)), norm(tuple(rem))


def gcd(a, b):
    """Monic greatest common divisor of two nonzero-or-zero polynomials."""
    while b:
        _, r = divmod_exact(a, b)
        a, b = b, r
    if not a:
        return ZERO
    return scale(a, 1 / a[-1])


def odd_part(p):
    """Factor a nonzero polynomial as (odd, square) with p == odd * square**2.

    odd is square-free: the product of the odd-indexed Yun square-free
    factors of p. Its real roots are exactly the sign-change (odd
    multiplicity) roots of p, so p(t) < 0 iff odd(t) < 0 wherever
    square(t) != 0.
    """
    if len(p) <= 1:
        return p, ONE
    c = gcd(p, deriv(p))
    w, rem = divmod_exact(p, c)
    assert not rem
    odd = ONE
    square = ONE
    i = 1
    while w != ONE:
        y = gcd(w, c)
        z, rem = divmod_exact(w, y)
        assert not rem
        if i % 2 == 1:
            odd = mul(odd, z)
            for _ in range((i - 1) // 2):
                square = mul(square, z)
        else:
            for _ in range(i // 2):
                square = mul(square, z)
        w = y
        c, rem = divmod_exact(c, y)
        assert not rem
        i += 1
    return odd, square


def sturm_chain(odd):
    """Sturm sequence of a nonzero square-free polynomial.

    Returns (chain, quotients): chain = [S0, ..., Sk] with S0 = odd,
    S1 = odd', S_{i-1} = quotients[i-1]*S_i - S_{i+1} (S_{k+1} = 0), and
    Sk a nonzero constant. len(quotients) == len(chain) - 1.
    """
    if not odd:
        raise ValueError('sturm chain of zero polynomial')
    chain = [odd]
    quotients = []
    if len(odd) > 1:
        chain.append(deriv(odd))
        while len(chain[-1]) > 1:
            q, r = divmod_exact(chain[-2], chain[-1])
            quotients.append(q)
            chain.append(neg(r))
        q, r = divmod_exact(chain[-2], chain[-1])
        assert not r  # square-free => last Sturm term is a nonzero constant
        quotients.append(q)
    return chain, quotients


def sign_variations(chain, x):
    """Sign variations of the chain evaluated at x, zeros skipped."""
    variations = 0
    previous = 0
    for s in chain:
        v = evaluate(s, x)
        if v == 0:
            continue
        sign = 1 if v > 0 else -1
        if previous and sign != previous:
            variations += 1
        previous = sign
    return variations


def roots_open(chain, a, b):
    """Number of distinct real roots of chain[0] in the open interval (a, b).

    Sturm's theorem for a square-free polynomial: V(a) - V(b) counts the
    roots in the half-open interval (a, b] (evaluations skip zeros, so the
    count is exact even when a or b is a root); subtract a root at b.
    """
    count = sign_variations(chain, a) - sign_variations(chain, b)
    if evaluate(chain[0], b) == 0:
        count -= 1
    return count


def locate_negative(odd, chain, a, b):
    """Rational point c in [a, b] with odd(c) < 0.

    Precondition: such a point exists in (a, b); odd is square-free and
    chain is its Sturm sequence. Bisection halves a rational interval that
    always contains a negative point, so it terminates.
    """
    if evaluate(odd, a) < 0:
        return a
    if evaluate(odd, b) < 0:
        return b
    lo, hi = a, b
    d_odd = deriv(odd)
    while True:
        mid = Fraction(lo + hi) / 2
        v = evaluate(odd, mid)
        if v < 0:
            return mid
        if v == 0:
            # Simple root: negative on exactly one side.
            if evaluate(d_odd, mid) > 0:
                hi = mid
            else:
                lo = mid
        elif sign_variations(chain, lo) - sign_variations(chain, mid) >= 1:
            hi = mid  # a sign-change root lies in (lo, mid)
        else:
            lo = mid  # otherwise one lies in (mid, hi)


def negative_witness(p, odd, chain, a, b):
    """Rational t in [a, b] with p(t) < 0. Precondition: such t exists."""
    if evaluate(p, a) < 0:
        return a
    if evaluate(p, b) < 0:
        return b
    c = locate_negative(odd, chain, a, b)
    if evaluate(p, c) < 0:
        return c
    # p(c) == 0 can only happen at an even-multiplicity root while odd(c) < 0;
    # p < 0 on a punctured neighbourhood of c, so halving steps find a point.
    k = 1
    while True:
        step = Fraction(b - a, 1 << k)
        for cand in (c + step, c - step):
            if a <= cand <= b and evaluate(p, cand) < 0:
                return cand
        k += 1
