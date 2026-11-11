"""F16 pass 2: the scan's fast std / Pearson helpers match numpy bit for bit (auto-sync results stay identical)."""
import warnings

import numpy as np

from amv_matcher._memo import pearson, pstd


def _cases():
    rng = np.random.default_rng(7)
    for m in list(range(2, 120)) + [301, 1000, 4097]:
        for t in range(8):
            a = rng.random(m) * (1e-4, 1.0, 80.0)[t % 3]
            b = rng.random(m)
            if t == 1:
                b = a + rng.random(m) * 1e-3
            if t == 2:
                b = -a
            if t == 3:
                a, b = np.round(a, 1), np.round(b, 1)
            if t == 4:
                a = np.full(m, 0.3)
            if t == 5:
                a = np.full(m, 0.1) + 1e-12 * rng.random(m)
            yield a, b


def test_pstd_matches_numpy_bitwise():
    for a, b in _cases():
        for x in (a, b, a[1:] if len(a) > 1 else a, b.astype(np.float32)):
            assert np.std(x) == pstd(x)


def test_pearson_matches_corrcoef_bitwise():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for a, b in _cases():
            r, g = np.corrcoef(a, b)[0, 1], pearson(a, b)
            assert r == g or (np.isnan(r) and np.isnan(g)), (len(a), r, g)
