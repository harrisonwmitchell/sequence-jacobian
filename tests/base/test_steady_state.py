"""Test all models' steady state computations"""

import numpy as np

from sequence_jacobian.models import rbc, krusell_smith, hank, two_asset


def test_rbc_steady_state(rbc_dag):
    _, _, _, _, ss = rbc_dag
    ss_ref = rbc.rbc_ss()
    assert set(ss.keys()) == set(ss_ref.keys())
    for k in ss.keys():
        assert np.all(np.isclose(ss[k], ss_ref[k]))


def test_ks_steady_state(krusell_smith_dag):
    _, _, _, _, ss = krusell_smith_dag
    ss_ref = krusell_smith.ks_ss(nS=2, nA=10, amax=200)
    assert set(ss.keys()) == set(ss_ref.keys())
    for k in ss.keys():
        assert np.all(np.isclose(ss[k], ss_ref[k]))


def test_hank_steady_state(one_asset_hank_dag):
    _, _, _, _, ss = one_asset_hank_dag
    ss_ref = hank.hank_ss(nS=2, nA=10, amax=150)
    assert set(ss.keys()) == set(ss_ref.keys())
    for k in ss.keys():
        assert np.all(np.isclose(ss[k], ss_ref[k]))


def test_two_asset_steady_state(two_asset_hank_dag):
    _, _, _, _, ss = two_asset_hank_dag
    ss_ref = two_asset.two_asset_ss(nZ=3, nB=10, nA=16, nK=4, verbose=False)
    assert set(ss.keys()) == set(ss_ref.keys())
    for k in ss.keys():
        assert np.all(np.isclose(ss[k], ss_ref[k]))
