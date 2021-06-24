"""This model is to test DisContBlock"""

import numpy as np
from numba import guvectorize, njit
import dfols

from sequence_jacobian import simple, agrid, markov_rouwenhorst, create_model, solved
from sequence_jacobian.blocks.discont_block import discont
from sequence_jacobian.utilities.misc import choice_prob, logsum
from sequence_jacobian.steady_state.classes import SteadyStateDict


'''Core HA block'''


def household_init(a_grid, z_grid, b_grid, atw, transfer, rpost, eis, vphi, chi):
    _, income = labor_income(z_grid, b_grid, atw, transfer, 0, 1, 1, 1)
    coh = income[:, :, np.newaxis] + (1 + rpost) * a_grid[np.newaxis, np.newaxis, :]
    c_guess = 0.3 * coh
    V, Va = np.empty_like(coh), np.empty_like(coh)
    for iw in range(4):
        V[iw, ...] = util(c_guess[iw, ...], iw, eis, vphi, chi)
    V = V / 0.1

    # get Va by finite difference
    Va[:, :, 1:-1] = (V[:, :, 2:] - V[:, :, :-2]) / (a_grid[2:] - a_grid[:-2])
    Va[:, :, 0] = (V[:, :, 1] - V[:, :, 0]) / (a_grid[1] - a_grid[0])
    Va[:, :, -1] = (V[:, :, -1] - V[:, :, -2]) / (a_grid[-1] - a_grid[-2])

    return Va, V


@njit(fastmath=True)
def util(c, iw, eis, vphi, chi):
    """Utility function."""
    # 1. utility from consumption.
    if eis == 1:
        u = np.log(c)
    else:
        u = c ** (1 - 1 / eis) / (1 - 1 / eis)

    # 2. disutility from work and search
    if iw == 0:
        u = u - vphi  # E
    elif (iw == 1) | (iw == 2):
        u = u - chi  # Ub, U

    return u


@discont(exogenous=('Pi_s', 'Pi_z'), policy='a', disc_policy='P', backward=('V', 'Va'), backward_init=household_init)
def household(V_p, Va_p, Pi_z_p, Pi_s_p, choice_set, a_grid, y_grid, z_grid, b_grid, lam_grid, eis, beta, rpost,
              vphi, chi):
    """
    Backward step function EGM with upper envelope.

    Dimensions: 0: labor market status, 1: productivity, 2: assets.
    Status: 0: E, 1: Ub, 2: U, 3: O
    State: 0: M, 1: B, 2: L

    Parameters
    ----------
    V_p         : array(Ns, Nz, Na), status-specific value function tomorrow
    Va_p        : array(Ns, Nz, Na), partial of status-specific value function tomorrow
    Pi_s_p      : array(Ns, Nx), Markov matrix for labor market shocks
    Pi_z_p      : array(Nz, Nz), (non-status-specific Markov) matrix for productivity
    choice_set  : list(Nz), discrete choices available in each state X
    a_grid      : array(Na), exogenous asset grid
    y_grid      : array(Ns, Nz), exogenous labor income grid
    z_grid      : array(Nz), productivity of employed (need for GE)
    b_grid      : array(Nz), productivity of unemployed (need for GE)
    lam_grid    : array(Nx), scale of taste shocks, specific to interim state
    eis         : float, EIS
    beta        : float, discount factor
    rpost       : float, ex-post interest rate
    vphi        : float, disutility of work
    chi         : float, disutility of search

    Returns
    -------
    V           : array(Ns, Nz, Na), status-specific value function today
    Va          : array(Ns, Nz, Na), partial of status-specific value function today
    P           : array(Nx, Ns, Nz, Na), probability of choosing status s in state x
    c           : array(Ns, Nz, Na), status-specific consumption policy today
    a           : array(Ns, Nz, Na), status-specific asset policy today
    ze          : array(Ns, Nz, Na), effective labor (average productivity if employed)
    ui          : array(Ns, Nz, Na), UI benefit claims (average productivity if unemployed)
    """
    # shapes
    Ns, Nz, Na = V_p.shape
    Nx = Pi_s_p.shape[1]

    # PART 1: update value and policy functions
    # a. discrete choice I expect to make tomorrow
    V_p_X = np.empty((Nx, Nz, Na))
    Va_p_X = np.empty((Nx, Nz, Na))
    for ix in range(Nx):
        V_p_ix = np.take(V_p, indices=choice_set[ix], axis=0)
        Va_p_ix = np.take(Va_p, indices=choice_set[ix], axis=0)
        P_p_ix = choice_prob(V_p_ix, lam_grid[ix])
        V_p_X[ix, ...] = logsum(V_p_ix, lam_grid[ix])
        Va_p_X[ix, ...] = np.sum(P_p_ix * Va_p_ix, axis=0)

    # b. compute expectation wrt labor market shock
    V_p1 = np.einsum('ij,jkl->ikl', Pi_s_p, V_p_X)
    Va_p1 = np.einsum('ij,jkl->ikl', Pi_s_p, Va_p_X)

    # b. compute expectation wrt productivity
    V_p2 = np.einsum('ij,kjl->kil', Pi_z_p, V_p1)
    Va_p2 = np.einsum('ij,kjl->kil', Pi_z_p, Va_p1)

    # d. consumption today on tomorrow's grid and endogenous asset grid today
    W = beta * V_p2
    uc_nextgrid = beta * Va_p2
    c_nextgrid = uc_nextgrid ** (-eis)
    a_nextgrid = (c_nextgrid + a_grid[np.newaxis, np.newaxis, :] - y_grid[:, :, np.newaxis]) / (1 + rpost)

    # e. upper envelope
    imin, imax = nonconcave(uc_nextgrid)  # bounds of non-concave region
    V, c = upper_envelope(imin, imax, W, a_nextgrid, c_nextgrid, a_grid, y_grid, rpost, eis, vphi, chi)

    # f. update Va
    uc = c ** (-1 / eis)
    Va = (1 + rpost) * uc

    # PART 2: things we need for GE

    # 2/a. asset policy
    a = (1 + rpost) * a_grid[np.newaxis, np.newaxis, :] + y_grid[:, :, np.newaxis] - c

    # 2/b. choice probabilities (don't need jacobian)
    P = np.zeros((Nx, Ns, Nz, Na))
    for ix in range(Nx):
        V_ix = np.take(V, indices=choice_set[ix], axis=0)
        P[ix, choice_set[ix], ...] = choice_prob(V_ix, lam_grid[ix])

    # 2/c. average productivity of employed
    ze = np.zeros_like(a)
    ze[0, ...] = z_grid[:, np.newaxis]

    # 2/d. UI claims
    ui = np.zeros_like(a)
    ui[1, ...] = b_grid[:, np.newaxis]

    return V, Va, a, c, P, ze, ui


def labor_income(z_grid, b_grid, atw, transfer, expiry, fU, fN, s):
    # 1. income
    yE = atw * z_grid + transfer
    yUb = atw * b_grid + transfer
    yN = np.zeros_like(yE) + transfer
    y_grid = np.vstack((yE, yUb, yN, yN))

    # 2. transition matrix for labor market status
    Pi_s = np.array([[1 - s, s, 0], [fU, (1 - fU) * (1 - expiry), (1 - fU) * expiry], [fU, 0, 1 - fU], [fN, 0, 1 - fN]])

    return Pi_s, y_grid


household.add_hetinput(labor_income, verbose=False)


"""Supporting functions for HA block"""


@guvectorize(['void(float64[:], uint32[:], uint32[:])'], '(nA) -> (),()', nopython=True)
def nonconcave(uc_nextgrid, imin, imax):
    """Obtain bounds for non-concave region."""
    nA = uc_nextgrid.shape[-1]
    vmin = np.inf
    vmax = -np.inf
    # step 1: find vmin & vmax
    for ia in range(nA - 1):
        if uc_nextgrid[ia + 1] > uc_nextgrid[ia]:
            vmin_temp = uc_nextgrid[ia]
            vmax_temp = uc_nextgrid[ia + 1]
            if vmin_temp < vmin:
                vmin = vmin_temp
            if vmax_temp > vmax:
                vmax = vmax_temp

    # 2/a Find imin (upper bound)
    if vmin == np.inf:
        imin_ = 0
    else:
        ia = 0
        while ia < nA:
            if uc_nextgrid[ia] < vmin:
                break
            ia += 1
        imin_ = ia

    # 2/b Find imax (lower bound)
    if vmax == -np.inf:
        imax_ = nA
    else:
        ia = nA
        while ia > 0:
            if uc_nextgrid[ia] > vmax:
                break
            ia -= 1
        imax_ = ia

    imin[:] = imin_
    imax[:] = imax_


@njit
def upper_envelope(imin, imax, W, a_nextgrid, c_nextgrid, a_grid, y_grid, rpost, *args):
    """
    Interpolate consumption and value function to exogenous grid. Brute force but safe.
    Parameters
    ----------
    W           : array(Ns, Nz, Na), status-specific end-of-period value function (on tomorrow's grid)
    a_nextgrid  : array(Ns, Nz, Na), endogenous asset grid (today's grid)
    c_nextgrid  : array(Ns, Nz, Na), consumption on endogenous grid (today's grid)
    a_grid      : array(Na), exogenous asset grid (tomorrow's grid)
    y_grid      : array(Ns, Nz), labor income
    rpost       : float, ex-post interest rate
    args        : (eis, vphi, chi) arguments for utility function
    Returns
    -------
    V  : array(Ns, Nz, Na), status-specific value function on exogenous grid
    c  : array(Ns, Nz, Na), consumption on exogenous grid
    """

    # 0. initialize
    Ns, Nz, Na = W.shape
    c = np.zeros_like(W)
    V = -np.inf * np.ones_like(W)

    # outer loop could run in parallel
    for iw in range(Ns):
        for iz in range(Nz):
            ycur = y_grid[iw, iz]
            imaxcur = imax[iw, iz]
            imincur = imin[iw, iz]

            # 1. unconstrained case: loop through a_grid, find bracketing endogenous gridpoints and interpolate.
            # in concave region: exploit monotonicity and don't look for extra solutions
            for ia in range(Na):
                acur = a_grid[ia]

                # Region 1: below non-concave: exploit monotonicity
                if (ia <= imaxcur) | (ia >= imincur):
                    iap = 0
                    ap_low = a_nextgrid[iw, iz, iap]
                    ap_high = a_nextgrid[iw, iz, iap + 1]
                    while iap < Na - 2:  # can this go up all the way?
                        if ap_high >= acur:
                            break
                        iap += 1
                        ap_low = ap_high
                        ap_high = a_nextgrid[iw, iz, iap + 1]
                    # found bracket, interpolate value function and consumption
                    w_low, w_high = W[iw, iz, iap], W[iw, iz, iap + 1]
                    c_low, c_high = c_nextgrid[iw, iz, iap], c_nextgrid[iw, iz, iap + 1]
                    w_slope = (w_high - w_low) / (ap_high - ap_low)
                    c_slope = (c_high - c_low) / (ap_high - ap_low)
                    c_guess = c_low + c_slope * (acur - ap_low)
                    w_guess = w_low + w_slope * (acur - ap_low)
                    V[iw, iz, ia] = util(c_guess, iw, *args) + w_guess
                    c[iw, iz, ia] = c_guess

                # Region 2: non-concave region
                else:
                    # try out all segments of endogenous grid
                    for iap in range(Na - 1):
                        # does this endogenous segment bracket ia?
                        ap_low, ap_high = a_nextgrid[iw, iz, iap], a_nextgrid[iw, iz, iap + 1]
                        interp = (ap_low <= acur <= ap_high) or (ap_low >= acur >= ap_high)

                        # does it need to be extrapolated above the endogenous grid?
                        # if needed to be extrapolated below, we would be in constrained case
                        extrap_above = (iap == Na - 2) and (acur > a_nextgrid[iw, iz, Na - 1])

                        if interp or extrap_above:
                            # interpolation slopes
                            w_low, w_high = W[iw, iz, iap], W[iw, iz, iap + 1]
                            c_low, c_high = c_nextgrid[iw, iz, iap], c_nextgrid[iw, iz, iap + 1]
                            w_slope = (w_high - w_low) / (ap_high - ap_low)
                            c_slope = (c_high - c_low) / (ap_high - ap_low)

                            # implied guess
                            c_guess = c_low + c_slope * (acur - ap_low)
                            w_guess = w_low + w_slope * (acur - ap_low)

                            # value
                            v_guess = util(c_guess, iw, *args) + w_guess

                            # select best value for this segment
                            if v_guess > V[iw, iz, ia]:
                                V[iw, iz, ia] = v_guess
                                c[iw, iz, ia] = c_guess

            # 2. constrained case: remember that we have the inverse asset policy a(a')
            ia = 0
            while ia < Na and a_grid[ia] <= a_nextgrid[iw, iz, 0]:
                c[iw, iz, ia] = (1 + rpost) * a_grid[ia] + ycur
                V[iw, iz, ia] = util(c[iw, iz, ia], iw, *args) + W[iw, iz, 0]
                ia += 1

    return V, c


'''Simple blocks'''


@simple
def income_state_vars(mean_z, rho_z, sd_z, nZ, uirate, uicap):
    # productivity
    z_grid, pi_z, Pi_z = markov_rouwenhorst(rho=rho_z, sigma=sd_z, N=nZ)
    z_grid *= mean_z

    # unemployment benefits
    b_grid = uirate * z_grid
    b_grid[b_grid > uicap] = uicap
    return z_grid, b_grid, pi_z, Pi_z


@simple
def employment_state_vars(lamM, lamB, lamL):
    choice_set = [[0, 3], [1, 3], [2, 3]]
    lam_grid = np.array([lamM, lamB, lamL])
    return choice_set, lam_grid


@simple
def asset_state_vars(amin, amax, nA):
    a_grid = agrid(amin=amin, amax=amax, n=nA)
    return a_grid


'''Rest of the model'''


@simple
def firm(Z, K, L, mc, alpha, delta0, delta1, psi):
    Y = Z * K(-1) ** alpha * L ** (1 - alpha)
    w = (1 - alpha) * mc * Y / L
    u = (alpha / delta0 / delta1 * mc * Y / K(-1)) ** (1 / delta1)
    delta = delta0 * u ** delta1
    Q = 1 + psi * (K / K(-1) - 1)
    I = K - (1 - delta) * K(-1) + psi / 2 * (K / K(-1) - 1) ** 2 * K(-1)
    transfer = Y - w * L - I
    return Y, w, u, delta, Q, I, transfer


@simple
def monetary(pi, rstar, phi_pi):
    # rpost = (1 + rstar(-1) + phi_pi * pi(-1)) / (1 + pi) - 1
    rpost = rstar
    return rpost


@simple
def nkpc(pi, mc, eps, Y, rpost, kappa):
    nkpc_res = kappa * (mc - (eps - 1) / eps) + Y(+1) / Y * np.log(1 + pi(+1)) / (1 + rpost(+1)) - np.log(1 + pi)
    return nkpc_res


@simple
def valuation(rpost, mc, Y, K, Q, delta, psi, alpha):
    val = alpha * mc(+1) * Y(+1) / K - (K(+1) / K - (1 - delta) + psi / 2 * (K(+1) / K - 1) ** 2) + \
          K(+1) / K * Q(+1) - (1 + rpost(+1)) * Q
    return val


@solved(unknowns={'B': [0.0, 10.0]}, targets=['budget'], solver='brentq')
def fiscal1(B, tax, w, rpost, G, Ze, Ui):
    budget = (1 + rpost) * B + G + (1 - tax) * w * Ui - tax * w * Ze - B
    # tax_rule = tax - tax.ss - phi * (B(-1) - B.ss) / Y.ss
    tax_rule = B - B.ss
    return budget, tax_rule


@simple
def fiscal2(w, tax):
    atw = (1 - tax) * w
    return atw


@solved(unknowns={'fU': 0.25, 'fN': 0.1, 's': 0.025}, targets=['fU_res', 'fN_res', 's_res'], solver='broyden_custom')
def flows(Y, fU, fN, s, fU_eps, fN_eps, s_eps):
    fU_res = fU.ss * (Y / Y.ss) ** fU_eps - fU
    fN_res = fN.ss * (Y / Y.ss) ** fN_eps - fN
    s_res = s.ss * (Y / Y.ss) ** s_eps - s
    return fU_res, fN_res, s_res


@simple
def mkt_clearing(A, B, Y, C, I, G, L, Ze):
    asset_mkt = A - B
    goods_mkt = Y - C - I - G
    labor_mkt = L - Ze
    return asset_mkt, goods_mkt, labor_mkt


@simple
def dividends(transfer):
    transfer_sm = transfer
    transfer_sw = transfer
    return transfer_sm, transfer_sw


@simple
def aggregate(A_sm, C_sm, Ze_sm, Ui_sm, A_sw, C_sw, Ze_sw, Ui_sw):
    A = (A_sm + A_sw) / 2
    C = (C_sm + C_sw) / 2
    Ze = (Ze_sm + Ze_sw) / 2
    Ui = (Ui_sm + Ui_sw) / 2
    return A, C, Ze, Ui


@simple
def helper1(A, tax, w, Ze, rpost, Ui):
    # after hh block
    B = A
    G = tax * w * Ze - rpost * B - (1 - tax) * w * Ui
    return B, G


@simple
def helper2(eps, rpost, Y, L, alpha, delta0):
    # uses u=1
    mc = (eps - 1) / eps
    K = mc * Y * alpha / (rpost + delta0)
    delta1 = alpha / delta0 * mc * Y / K
    Z = Y / (K ** alpha * L ** (1 - alpha))
    w = (1 - alpha) * mc * Y / L
    return mc, K, delta1, Z, w


'''Try this'''

cali_sm = {'beta': 0.9782, 'vphi': 0.8079, 'chi': 0.5690, 'fU': 0.25, 'fN': 0.1174, 's': 0.0218,
           'mean_z': 1.0, 'rho_z': 0.98, 'sd_z': 0.943*0.82,
           'fU_eps': 10.69, 'fN_eps': 5.57, 's_eps': -11.17}

cali_sw = {'beta': 0.9830, 'vphi': 0.9909, 'chi': 0.4982, 'fU': 0.22, 'fN': 0.1099, 's': 0.0132,
           'mean_z': 0.8, 'rho_z': 0.98, 'sd_z': 0.86*0.82,
           'fU_eps': 8.72, 'fN_eps': 3.55, 's_eps': -6.55}

cali_sm = {k + '_sm': v for k, v in cali_sm.items()}
cali_sw = {k + '_sw': v for k, v in cali_sw.items()}

calibration = {**cali_sm, **cali_sw,
               'eis': 1.0, 'uicap': 0.66, 'uirate': 0.5, 'expiry': 1/6, 'eps': 10.0, 'tax': 0.3,
               'amin': 0.0, 'amax': 500, 'nA': 100, 'lamM': 0.01, 'lamB': 0.01, 'lamL': 0.04, 'nZ': 7,
               'kappa': 0.03, 'phi_pi': 1.25, 'rstar': 0.002, 'pi': 0.0, 'alpha': 0.2, 'psi': 30, 'delta0': 0.0083}

hh = create_model([income_state_vars, employment_state_vars, asset_state_vars, flows, household], name='Single')


to_map = ['beta', 'vphi', 'chi', 'fU', 'fN', 's', 'mean_z', 'rho_z', 'sd_z', 'transfer', 'fU_eps', 'fN_eps', 's_eps',
          *hh.outputs]
hh_sm = hh.remap({k: k + '_sm' for k in to_map}).rename('SingleMen')
hh_sw = hh.remap({k: k + '_sw' for k in to_map}).rename('SingleWomen')


hank = create_model([hh_sm, hh_sw, aggregate, dividends, firm, monetary, valuation, nkpc, fiscal1, fiscal2,
                     mkt_clearing], name='HANK')

ss = hank.solve_steady_state(calibration, solver='brentq', dissolve=[fiscal1, flows],
                             unknowns={'L': (0.7, 0.72),
                                       'Z': 0.63, 'mc': 0.9, 'G': 0.2, 'delta1': 1.2, 'B': 3.0, 'K': 17.0},
                             targets={'labor_mkt': 0.0,
                                      'Y': 1.0, 'nkpc_res': 0.0, 'budget': 0.0, 'asset_mkt': 0.0, 'val': 0.0, 'u': 1.0},
                             helper_blocks=[helper1, helper2],
                             helper_targets=['asset_mkt', 'budget', 'val', 'nkpc_res', 'Y', 'u'])

out = hh_sm.steady_state(ss)


