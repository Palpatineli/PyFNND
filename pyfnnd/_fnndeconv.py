import numpy as np
from scipy import signal
from itertools import izip
import time
import warnings
from _tridiag_solvers import trisolve
from utils import s2h

DTYPE = np.float64
EPS = np.finfo(DTYPE).eps


# joblib is an optional dependency, required only for processing multiple cells
# in parallel
try:
    from joblib import Parallel, delayed

    def apply_all_cells(F, n_jobs=-1, disp=1, *fnn_args, **fnn_kwargs):
        """
        Run FNN deconvolution on multiple cells in parallel

        Arguments:
        -----------------------------------------------------------------------
        F: ndarray, [nc, nt] or [nc, npix, nt]
            measured fluorescence values

        n_jobs: int scalar
            number of jobs to process in parallel. if n_jobs == -1, all cores
            are used.

        *fnn_args, **fnn_kwargs
            additional arguments to pass to deconvolve()

        Returns:
        -----------------------------------------------------------------------
        n_hat_best: ndarray, [nc, nt]
            MAP estimate of the most likely spike train

        c_hat_best: ndarray, [nc, nt]
            estimated intracellular calcium concentration (A.U.)

        LL: ndarray, [nc,]
            posterior log-likelihood of F given n_hat_best and theta_best

        theta_best: tuple, [nc,]
            model parameters, updated according to learn_theta
        """

        pool = Parallel(n_jobs=n_jobs, verbose=disp, pre_dispatch='n_jobs * 2')

        results = pool(delayed(deconvolve)
                       (rr, *fnn_args, **fnn_kwargs) for rr in F)

        n_hat, c_hat, LL, theta = izip(*results)
        n_hat, c_hat, LL = (np.vstack(a) for a in (n_hat, c_hat, LL))

        return n_hat, c_hat, LL, theta

except ImportError:
    # apply_all_cells is left undefined if joblib isn't present
    pass


def deconvolve(F, c0=None, theta0=((None,) * 5), dt=0.02, rate=0.5, tau=1.,
               learn_theta=((0,) * 5), norm_alpha=True, params_tol=1E-3,
               spikes_tol=1E-3, spikes_maxiter=100, params_maxiter=20,
               decimate=0, verbosity=0):
    """

    Fast Non-Negative Deconvolution
    ---------------------------------------------------------------------------
    This function uses an interior point method to solve the following
    optimization problem:

        n_hat = argmax_{n >= 0} P(n | F)

    where n_hat_best is a maximum a posteriori estimate for the most likely
    spike train, given the fluorescence signal F, and the model:

    c_{t} = gamma * c_{t-1} + n_{t},            n_{t} ~ Poisson(lambda * dt)
    F_{t} = alpha * c_{t} + beta + epsilon,     epsilon ~ N(0, sigma)

    It is also possible to estimate the model parameters sigma, alpha, beta and
    lambda from the data using pseudo-EM updates.

    Arguments:
    ---------------------------------------------------------------------------
    F: ndarray, [nt] or [npix, nt]
        measured fluorescence values

    c0: ndarray, [nt]
        initial estimate of the calcium concentration for each time bin

    theta0: len(5) sequence
        initial estimates of the model parameters (sigma, alpha, beta, lambda,
        gamma). any parameters in theta0 that are None will be estimated from
        F.

    dt: float scalar
        duration of each time bin (s)

    rate: float scalar
        estimate of mean firing rate (Hz), ignored if theta0[3] is not None

    tau: float scalar
        estimate of calcium decay time constant (s), ignored if theta0[4] is
        not None

    learn_theta: len(5) bool sequence
        specifies which of the model parameters to attempt learn via pseudo-EM
        iterations. currently gamma cannot be optimised.

    norm_alpha: bool
        if True (default), we impose that sum(alpha) == 1 by scaling n_hat and
        c_hat

    spikes_tol: float scalar
        termination condition for interior point spike train estimation:
            params_tol > abs((LL_prev - LL) / LL)

    params_tol: float scalar
        as above, but for the model parameter estimation

    spikes_maxiter: int scalar
        maximum number of interior point iterations to estimate MAP spike train

    params_maxiter: int scalar
        maximum number of pseudo-EM iterations to estimate model parameters

    decimate: int scalar
        option to decimate the input array over time when initalizing and
        updating the theta parameters, i.e. Fd = F[:, ::decimate]. this can
        be useful for very large input arrays. the default value (0) means no
        decimation is performed.

    verbosity: int scalar
        0: no convergence messages (default)
        1: convergence messages for model parameters
        2: convergence messages for model parameters & MAP spike train

    Returns:
    ---------------------------------------------------------------------------
    n_hat_best: ndarray, [nt]
        MAP estimate of the most likely spike train

    c_hat_best: ndarray, [nt]
        estimated intracellular calcium concentration (A.U.)

    LL_best: float scalar
        posterior log-likelihood of F given n_hat_best and theta_best

    theta_best: len(5) tuple
        model parameters, updated according to learn_theta

    Reference:
    ---------------------------------------------------------------------------
    Vogelstein, J. T., Packer, A. M., Machado, T. A., Sippy, T., Babadi, B.,
    Yuste, R., & Paninski, L. (2010). Fast nonnegative deconvolution for spike
    train inference from population calcium imaging. Journal of
    Neurophysiology, 104(6), 3691-704. doi:10.1152/jn.01073.2009

    """

    tstart = time.time()

    F = np.atleast_2d(F.astype(DTYPE))
    npix, nt = F.shape

    # ensure that F is non-negative
    offset = F.min() - EPS
    F = F - offset

    theta = _init_theta(F, theta0, offset, dt=dt, rate=rate, tau=tau,
                        decimate=decimate)

    sigma, alpha, beta, lamb, gamma = theta

    if c0 is None:

        # let n0 be a uniform vector of estimated mean spike probability, then
        # push this through the forward model to get c0. this way c0 is
        # guaranteed not to result in negative spike probabities in n_hat on
        # the first iteration.
        n0 = lamb * dt * np.ones(nt, dtype=DTYPE)
        c0 = signal.lfilter(np.r_[1.], np.r_[1., -gamma], n0, axis=0)

    # if we're not learning the parameters, this step is all we need to do
    n_hat, c_hat, LL = _get_MAP_spikes(F, c0, theta, dt, spikes_tol,
                                       spikes_maxiter, verbosity)

    # pseudo-EM iterations to optimize the model parameters
    if any(learn_theta):

        if verbosity >= 1:
            print('params: iter=%3i; LL=%-10.4g; delta_LL= N/A' % (0, LL))

        nloop1 = 1
        done = False

        while not done:

            s = 1.
            nloop2 = 1
            terminate_linesearch = False

            # a 'full' parameter update, as used in the Vogelstein paper/code
            theta_up = _update_theta(n_hat, c_hat, F, theta, dt, learn_theta,
                                     decimate)

            # we might want to make changes to a copy of c_hat in the
            # linesearch to get out of local minima
            c_hat0 = c_hat.copy()

            # backtracking linesearch for the biggest step size that improves
            # the LL
            while not terminate_linesearch:

                # increment the parameter values according to the current step
                # size
                theta1 = tuple(
                    (p + (p1 - p) * s for p, p1 in izip(theta, theta_up))
                )

                # get the new n_hat, c_hat, and LL
                n1, c_hat1, LL1 = _get_MAP_spikes(
                    F, c_hat0, theta1, dt, spikes_tol, spikes_maxiter,
                    verbosity
                )

                # new solution found
                if LL1 >= LL:
                    terminate_linesearch = True

                # terminate if the step size gets too small without seeing any
                # improvement in LL
                elif s < 0.01:
                    if verbosity >= 1:
                        print('params: terminated linesearch: s < 0.01 on'
                              ' %i iterations' % nloop2)
                    terminate_linesearch = True
                    done = True

                else:
                    # in order to get out of local minima it can be helpful to
                    # increase c_hat a bit to ensure that n_hat is non-zero
                    # everywhere
                    c_hat0 *= 1.5

                    # reduce the step size, increment the counter
                    s /= 2.
                    nloop2 += 1

            # test for convergence
            delta_LL = -((LL1 - LL) / LL)

            if verbosity >= 1:
                print('params: iter=%3i; LL=%-10.4g; delta_LL=%-10.4g'
                      % (nloop1, LL1, delta_LL))

            if delta_LL > 0:

                # keep the new parameters
                n_hat, c_hat, LL, theta = n1, c_hat1, LL1, theta1

                # if the LL is not improving significantly, time to terminate
                if delta_LL < params_tol:
                    if verbosity >= 1:
                        print("Parameter optimization converged after %i "
                              "iterations" % nloop1)
                    done = True

                elif nloop1 == params_maxiter:
                    if verbosity >= 1:
                        print('Parameter optimization failed to converge '
                              'before maxiter was reached (%i)' % nloop1)
                    done = True

            else:
                if verbosity >= 1:
                    print('Terminating parameter optimization on %i '
                          'iterations: LL is decreasing' % nloop1)
                done = True

            if done:
                if verbosity >= 1:
                    print "Last delta log-likelihood:\t%-10.4g" % delta_LL
                    print "Best posterior log-likelihood:\t%10.3f" % LL

            # increment the loop counter
            nloop1 += 1

    if verbosity >= 1:
        time_taken = time.time() - tstart
        print "Completed: %s" % s2h(time_taken)

    sigma, alpha, beta, lamb, gamma = theta

    # we can impose that sum(alpha) == 1 by scaling c_hat and n_hat to
    # compensate
    if norm_alpha:
        alpha_sum = np.sum(alpha)
        alpha /= alpha_sum
        c_hat *= alpha_sum

        # needs to be constrained - n_hat can be negative if sum(alpha) is
        # negative!
        n_hat = c_hat[1:] - gamma * c_hat[:-1]
        # assert not np.any(n_hat < 0), "spike probabilities < 0"

    # correct for the offset we originally applied to F
    beta = beta + offset

    # since we can't use FNND to estimate the spike probabilities in the 0th
    # timebin, for convenience we just concatenate 0 to the start of
    # n_hat so that it has the same shape as F and c_hat
    n_hat = np.r_[0, n_hat]

    theta = sigma, alpha, beta, lamb, gamma

    return n_hat, c_hat, LL, theta


def _get_MAP_spikes(F, c_hat, theta, dt, tol=1E-6, maxiter=100, verbosity=0):
    """
    Used internally by deconvolve to compute the maximum a posteriori
    spike train for a given set of fluorescence traces and model parameters.

    See the documentation for deconvolve for the meaning of the
    arguments

    Returns:    n_hat_best, c_hat_best, LL_best

    """
    npix, nt = F.shape

    sigma, alpha, beta, lamb, gamma = theta

    # we project everything onto the alpha mask so that we only ever have to
    # deal with 1D vector norms
    alpha_F = alpha.dot(F)
    alpha_ss = alpha.dot(alpha)
    alpha_beta = alpha.dot(beta)
    alpha_F_bl = alpha_F - alpha_beta

    F_bl = F - beta[:, None]

    # used for computing the LL and gradient
    scale_var = 1. / (2 * sigma * sigma)
    lD = lamb * dt

    # used for computing the gradient (M.T.dot(lamb * dt))
    grad_lnprior = np.zeros(nt, dtype=DTYPE)
    grad_lnprior[1:] = lD
    grad_lnprior[:-1] -= lD * gamma

    # initial estimate of spike probabilities (should be strictly non-negative)
    n_hat = c_hat[1:] - gamma * c_hat[:-1]
    # assert not np.any(n_hat < 0), "spike probabilities < 0"

    # (actual - predicted) fluorescence
    D = F_bl - alpha[:, None] * c_hat[None, :]

    # initialize the weight of the barrier term to 1
    z = 1.

    # compute initial posterior log-likelihood of the fluorescence
    LL = _post_LL(n_hat, D, scale_var, lD, z)

    nloop1 = 0
    LL_prev, c_hat_prev = LL, c_hat
    terminate_interior = False

    # in the outer loop we'll progressively reduce the weight of the barrier
    # term and check the interior point termination criteria
    while not terminate_interior:

        s = 1.
        d = 1.
        nloop2 = 0

        # converge for this barrier weight
        while (np.linalg.norm(d) > 5E-2) and (s > 1E-3):

            # by projecting everything onto alpha, we reduce this to a 1D
            # vector norm
            res = alpha_F_bl - alpha_ss * c_hat

            # compute direction of newton step
            d = _direction(n_hat, res, alpha_ss, gamma, scale_var,
                           grad_lnprior, z)

            terminate_linesearch = False

            # ensure that step size starts sufficiently small to guarantee that
            # n_hat stays positive
            hit = -n_hat / (d[1:] - gamma * d[:-1])
            within_bounds = (hit >= EPS)

            if np.any(within_bounds):
                s = min(1., 0.99 * np.min(hit[within_bounds]))
            else:
                # force an early termination at this barrier weight if there is
                # no step size that will keep n_hat >= 0
                terminate_linesearch = True
                s = -1
                terminate_interior = True
                if verbosity >= 2:
                    print("terminating: no step size will keep n_hat >= 0")

            nloop3 = 0

            # backtracking line search for the largest step size that increases
            # the posterior log-likelihood of the fluorescence
            while not terminate_linesearch:

                # update estimated calcium
                c_hat1 = c_hat + (s * d)

                # update spike probabilities
                n_hat = c_hat1[1:] - gamma * c_hat1[:-1]
                # assert not np.any(n_hat < 0), "spike probabilities < 0"

                # (actual - predicted) fluorescence
                D = F_bl - alpha[:, None] * c_hat1[None, :]

                # compute the new posterior log-likelihood
                LL1 = _post_LL(n_hat, D, scale_var, lD, z)
                # assert not np.any(np.isnan(LL1)), "nan LL"

                if verbosity >= 2:
                    print('spikes: iter=%3i, %3i, %3i; z=%-10.4g; s=%-10.4g;'
                          ' LL=%-10.4g'
                          % (nloop1, nloop2, nloop3, z, s, LL1))

                # only update c_hat & LL if LL improved
                if LL1 > LL:
                    LL, c_hat = LL1, c_hat1
                    terminate_linesearch = True

                # terminate when the step size gets too small without making
                # progress
                elif s < 1E-3:
                    if verbosity >= 2:
                        print('terminated linesearch: s < 1E-3 on %i '
                              'iterations' % nloop3)
                    terminate_linesearch = True

                else:
                    # reduce the step size
                    s /= 2.
                    nloop3 += 1

            nloop2 += 1

        # test for convergence
        delta_LL = -(LL - LL_prev) / LL_prev

        # keep new params
        LL_prev, c_hat_prev = LL, c_hat

        # increment the outer loop counter, reduce the barrier weight
        nloop1 += 1
        z /= 2.

        if (delta_LL < tol):
            terminate_interior = True

        elif z < 1E-6:
            if verbosity >= 2:
                print 'MAP spike train failed to converge before z < 1E-6'
            terminate_interior = True

        elif nloop1 > maxiter:
            if verbosity >= 2:
                print('MAP spike train failed to converge within maxiter (%i)'
                       % maxiter)
            terminate_interior = True

    return n_hat, c_hat, LL


def _post_LL(n_hat, D, scale_var, lD, z):

    # barrier term
    with np.errstate(invalid='ignore'):     # suppress log(0) error messages
        barrier = np.log(n_hat).sum()       # this is currently a bottleneck

    # sum of squared (predicted - actual) fluorescence
    D_ss = D.ravel().dot(D.ravel())         # fast sum-of-squares

    # weighted posterior log-likelihood of the fluorescence
    LL = -(scale_var * D_ss) - (n_hat.sum() / lD) + (z * barrier)

    return LL


def _direction(n_hat, res, alpha_ss, gamma, scale_var,
               grad_lnprior, z):

    nt = grad_lnprior.shape[0]

    # gradient
    n_term = np.zeros(nt, dtype=DTYPE)
    n_term[:(nt - 1)] = -gamma / n_hat
    n_term[-(nt - 1):] += 1. / n_hat
    g = (2 * scale_var * res - grad_lnprior + z * n_term)

    # main diagonal of the hessian
    n2 = n_hat * n_hat
    Hd0 = np.zeros(nt, dtype=DTYPE)
    Hd0[:(nt - 1)] = (gamma * gamma) / n2
    Hd0[-(nt - 1):] += 1 / n2
    Hd0 *= -z
    Hd0 -= 2 * alpha_ss * scale_var

    # upper/lower diagonals of the hessian
    Hd1 = z * gamma / n2

    # solve the tridiagonal system Hd = -g (we use -g, since we want to
    # *ascend* the LL gradient)
    d = trisolve(Hd1, Hd0, Hd1.copy(), -g, inplace=True)

    return d


def _update_theta(n_hat, c_hat, F, theta, dt, learn_theta, decimate=0):

    if decimate > 0:
        c_hat = c_hat[::decimate]
        n_hat = n_hat[::decimate]
        F = F[:, ::decimate]

    sigma, alpha, beta, lamb, gamma = theta
    learn_sigma, learn_alpha, learn_beta, learn_lamb, learn_gamma = learn_theta

    npix, nt = F.shape

    if learn_alpha:

        if learn_beta:
            A = np.vstack((c_hat, np.ones(nt, dtype=DTYPE)))
        else:
            A = c_hat[None, :]

        Y, residuals, rank, singular_vals = np.linalg.lstsq(A.T, F.T)

        # # enforce non-negativity of coefficients?
        # Y[Y < EPS] = EPS

        if learn_beta:
            alpha, beta = Y

        else:
            alpha = Y[0]

    elif learn_beta:
        beta = (F - alpha[:, None] * c_hat[None, :]).sum(1) / nt

    if learn_sigma:
        D = F - (alpha[:, None] * c_hat[None, :] - beta[:, None])
        ssd = D.ravel().dot(D.ravel())      # fast sum-of-squares
        sigma = np.sqrt(ssd / nt)           # RMS error

    if learn_lamb:
        lamb = n_hat.sum() / (nt * dt)

    if learn_gamma:
        warnings.warn('optimising gamma is not yet supported (ignoring)')

    return (sigma, alpha, beta, lamb, gamma)


def _init_theta(F, theta0, offset, dt=0.02, rate=1., tau=1.0, decimate=0):

    if decimate > 0:
        F = F[:, ::decimate]

    npix = F.shape[0]
    sigma, alpha, beta, lamb, gamma = theta0

    if None in (sigma, alpha, beta):
        med_F = np.median(F, axis=1)

    # noise parameter
    if sigma is None:
        # K is the correction factor when using the median absolute deviation
        # as a robust estimator of the standard deviation of a normal
        # distribution <http://en.wikipedia.org/wiki/Median_absolute_deviation>
        K = 1.4826
        mad = np.median(np.abs(F - med_F[:, None]))
        sigma = mad * K                         # scalar

    # amplitude
    if alpha is None:
        alpha = np.atleast_1d(med_F)            # vector

    # baseline
    if beta is None:
        if npix == 1:
            beta = np.atleast_1d(np.percentile(F, 5., axis=1))
        else:
            beta = np.atleast_1d(med_F)
    else:
        # beta should absorb the offset parameter
        beta = np.atleast_1d(beta - offset)

    # firing rate
    if lamb is None:
        lamb = rate                             # scalar

    # decay parameter (fraction of remaining fluorescence after one time step)
    if gamma is None:
        gamma = np.exp(-dt / tau)               # scalar

    return sigma, alpha, beta, lamb, gamma
