# Copyright 2020 Petuum, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import autograd
import numpy as np
import collections
import scipy.optimize
import scipy.stats


# Parameters for a performance model which predicts the per-step time of
# distributed SGD using all-reduce. At a high level, models compute time and
# network time separately, and combines them with some degree of overlap.
# Compute time is modeled as a linear function of the local batch size.
# Network time is modeled using different parameters depending on if the job
# is inter-node (there exists a pair of replicas on different nodes), or
# intra-node (all replicas are on the same node). For both cases, network time
# is modeled as a constant term plus a retrogression term which increases
# linearly with the total number of replicas.
Params = collections.namedtuple("Params", [
    # T_compute ~ alpha_c + beta_c * local_bsz
    "alpha_c",  # Constant term of compute time
    "beta_c",   # Multiplicative factor of compute time
    # If inter-node: T_network ~ alpha_n + beta_n * replicas
    "alpha_n",  # Constant term of inter-node network time
    "beta_n",   # Retrogression factor of inter-node network time
    # If intra-node: T_network ~ alpha_r + beta_r * replicas
    "alpha_r",  # Constant term of intra-node network time
    "beta_r",   # Retrogression factor of intra-node network time
    # T_step ~ (T_compute ^ gamma + T_network ^ gamma) ^ (1 / gamma)
    # Essentially is a p-norm where p = gamma. When p ~ 1 then
    # T_step ~ T_compute + T_network, indicating no overlap between compute
    # and network. When p -> infinity then T_step = max(T_compute, T_network),
    # indicating perfect overlap. We limit gamma to [1, 10] since 10 is close
    # enough to approximate the max function for our purposes.
    "gamma",    # Models the degree of overlap between compute and network
])


class SpeedupFunction(object):

    def __init__(self, params, grad_params=None, init_batch_size=None,
                 max_batch_size=None, local_bsz_bounds=None,
                 elastic_bsz=False):
        self._grad_params = grad_params
        self._init_batch_size = init_batch_size

        if local_bsz_bounds is not None:
            self._max_local_bsz = local_bsz_bounds[1]
            self._min_local_bsz = local_bsz_bounds[0]
        else:
            self._max_local_bsz = None
            self._min_local_bsz = None

        default_max_batch_size_scale = 100
        if max_batch_size is not None:
            self._max_batch_size = max_batch_size
        elif elastic_bsz:
            self._max_batch_size = (default_max_batch_size_scale *
                                    init_batch_size)
        else:
            self._max_batch_size = init_batch_size
        if params is not None:
            self._params = Params(*params)
        else:
            self._params = None
        if params is not None and init_batch_size is not None:
            base_step_time, _, _ = _predict_log(self._params,
                                                np.array([1]), np.array([1]),
                                                init_batch_size)
            base_step_time = base_step_time.item()
            self._base_goodput = 1.0 / np.exp(base_step_time)
        else:
            self._base_goodput = 1.0
        self._elastic_bsz = elastic_bsz
        # Memoization for fast repeated queries.
        self._mem_size = 32
        self._mem_speedup = np.full((self._mem_size, self._mem_size), -1.0)
        self._mem_local_bsz = np.full((self._mem_size, self._mem_size), -1)
        self._mem_speedup[0, 0] = 0.0  # replicas = 0  ==>  speedup = 0
        self._mem_local_bsz[0, 0] = 0
        self._mem_speedup[1, 1] = 1.0  # replicas = 1  ==>  speedup = 1
        self._mem_local_bsz[1, 1] = self._init_batch_size

    def __call__(self, nodes, replicas, return_local_bsz=False):
        # nodes and replicas must have the same shape, dtype=int
        assert np.shape(nodes) == np.shape(replicas)
        assert np.all(np.less_equal(0, nodes))
        assert np.all(np.less_equal(nodes, replicas))
        assert np.all((nodes > 0) == (replicas > 0))

        # Remember if original arguments are scalars.
        isscalar = np.isscalar(replicas)
        nodes, replicas = np.atleast_1d(nodes, replicas)

        # Return values which will be filled out.
        ret_speedup = np.full(np.shape(replicas), -1.0)
        ret_local_bsz = np.full(np.shape(replicas), -1)

        # Fill in any memoized results first.
        ret_indices = replicas < self._mem_size
        mem_indices = (nodes[ret_indices], replicas[ret_indices])
        ret_speedup[ret_indices] = self._mem_speedup[mem_indices]
        ret_local_bsz[ret_indices] = self._mem_local_bsz[mem_indices]

        # Find the indices which still need to be computed.
        indices = ret_speedup < 0
        nodes, replicas = nodes[indices], replicas[indices]

        # Only compute for unique inputs.
        if np.size(replicas) > 0:
            (nodes, replicas), unique_indices = np.unique(
                    np.stack([nodes, replicas]), axis=1, return_inverse=True)
        else:
            unique_indices = np.array([], dtype=np.int)

        if np.size(replicas) == 0:
            local_bsz = np.array([], dtype=np.int)
            goodput = np.array([])
        elif self._params is None:
            local_bsz = np.ceil(self._init_batch_size / replicas).astype(int)
            goodput = np.ones(np.shape(replicas))
        elif self._elastic_bsz:
            max_local_bsz = np.floor(self._max_batch_size / replicas)
            min_local_bsz = np.ceil(self._init_batch_size / replicas)
            if self._max_local_bsz is not None:
                max_local_bsz = np.minimum(self._max_local_bsz, max_local_bsz)
            if self._min_local_bsz is not None:
                min_local_bsz = np.maximum(self._min_local_bsz, min_local_bsz)
            assert np.all(max_local_bsz >= min_local_bsz)
            # Sample a bunch of potential local_bsz values
            local_bsz = np.geomspace(min_local_bsz, max_local_bsz, num=100)
            # Should get broadcast to (num_samples, replicas.size).
            goodput = self._goodput(nodes, replicas, local_bsz)
            local_bsz = local_bsz[np.argmax(goodput, axis=0),
                                  np.arange(local_bsz.shape[1])]
            local_bsz = local_bsz.round().astype(int)
            goodput = np.amax(goodput, axis=0)
        else:
            local_bsz = np.ceil(self._init_batch_size / replicas).astype(int)
            log_pred_step_time, _, _ = \
                _predict_log(self._params, nodes, replicas, local_bsz)
            goodput = 1.0 / np.exp(log_pred_step_time)
        speedup = goodput / self._base_goodput

        # Undo unique.
        nodes = nodes[unique_indices]
        replicas = replicas[unique_indices]
        speedup = speedup[unique_indices]
        local_bsz = local_bsz[unique_indices]

        # Fill in computed results.
        ret_speedup[indices] = speedup
        ret_local_bsz[indices] = local_bsz

        # Memoize results.
        ret_indices = replicas < self._mem_size
        mem_indices = (nodes[ret_indices], replicas[ret_indices])
        self._mem_speedup[mem_indices] = speedup[ret_indices]
        self._mem_local_bsz[mem_indices] = local_bsz[ret_indices]

        if isscalar:
            ret_speedup = ret_speedup.item()
            ret_local_bsz = ret_local_bsz.item()

        return ((ret_speedup, ret_local_bsz)
                if return_local_bsz else ret_speedup)

    def _goodput(self, nodes, replicas, local_bsz):
        log_pred_step_time, _, _ = \
            _predict_log(self._params, nodes, replicas, local_bsz)
        var, norm = self._grad_params['var'], self._grad_params['norm']
        global_bsz = replicas * local_bsz
        gain = np.where(
            (var / global_bsz * self._init_batch_size + norm) == 0.0,
            1.0,
            (var + norm) / (var / global_bsz * self._init_batch_size + norm))
        return gain / np.exp(log_pred_step_time)

    def params(self):
        return self._params


def fit(nodes, replicas, local_bsz, step_time, step_time_compute):
    # Fit the performance model given step time and compute time measurements
    # for different configurations of nodes, replicas, local_bsz.

    # HACK: We want to use the original numpy module for calls from the
    # SpeedupFunction for performance reasons, but also need those functions to
    # use autograd.numpy when we want to differentiate them. We patch the
    # global np reference only for the code invoked rom this function.
    global np  # Replace numpy from autograd.
    orig_np = np
    np = autograd.numpy

    replicas = np.array(replicas)
    local_bsz = np.array(local_bsz)
    step_time = np.array(step_time)
    step_time_compute = np.array(step_time_compute)

    # Set initial params to reasonable values.
    params = [1e-1, 1e-2] * 3 + [1.0 + 1e-3]
    # Set lower/upper bounds for each parameter. Add a small slack to lower
    # bounds to avoid numerical instability issues.
    lower = [1e-8, 1e-8] * 3 + [1.0]
    upper = [np.inf, np.inf] * 3 + [10.0]
    if len(np.unique(local_bsz)) == 1:
        # Fix alpha_c if only observed a single local batch size.
        params[0] = upper[0] = lower[0]
    if not any(nodes > 1):
        # Fix alpha_n and beta_n if no multi-node observations.
        params[2] = upper[2] = lower[2]
        params[3] = upper[3] = lower[3]
    if not any(np.logical_and(nodes == 1, replicas > 1)):
        # Fix alpha_r and beta_r if no single-node/multi-replica observations.
        params[4] = upper[4] = lower[4]
        params[5] = upper[5] = lower[5]
    if not any(replicas > 2):
        # Fix beta_n and beta_r if no replicas > 2.
        params[3] = upper[3] = lower[3]
        params[5] = upper[5] = lower[5]
    bounds = scipy.optimize.Bounds(lower, upper, keep_feasible=True)

    args = (nodes, replicas, local_bsz, step_time, step_time_compute)
    # FIXME: need to handle optimization failures and propagate to the Trainer.
    grad_fn = autograd.grad(_obj_fn)
    result = scipy.optimize.minimize(_obj_fn, params, args=args,
                                     jac=grad_fn, bounds=bounds)
    params = result.x

    np = orig_np  # Restore original numpy.
    return Params(*params)


def _predict_log(params, nodes, replicas, local_bsz):
    params = Params(*params)
    step_time_compute = _predict_compute(params, local_bsz)
    step_time_network = _predict_network(params, nodes, replicas)
    gamma = params.gamma
    # Return predicted total step time in log-space to avoid numerical issues
    # in autograd and optimization.
    return (np.log(step_time_compute ** gamma +
                   step_time_network ** gamma) / gamma,
            step_time_compute, step_time_network)


def _predict_compute(params, local_bsz):
    params = Params(*params)
    # Forward/backward passes should scale linearly with the batch size.
    return params.alpha_c + params.beta_c * local_bsz


def _predict_network(params, nodes, replicas):
    params = Params(*params)
    # Select the most significant link between replicas, currently either
    # inter-node (nodes > 1) or intra-node (replicas > 1). Note that if
    # replicas == 1 then neither of these two conditions are matched.
    conds = [nodes > 1, replicas > 1]
    # Bandwidth is bottlenecked by the most significant link, alpha models
    # the overhead of transferring data across that link.
    bottleneck = np.select(conds, [params.alpha_n, params.alpha_r], 1e-8)
    # Assuming ring all-reduce, communication happens in a number of rounds
    # equal to the number of replicas. beta models the performance
    # retrogression from increasing the number of replicas beyond 2.
    retrogress = np.select(conds, [params.beta_n, params.beta_r], 1e-8)
    retrogress = retrogress * np.maximum(replicas - 2, 1e-8)
    return (bottleneck + retrogress)


def _rmse(pred, true):
    return np.sqrt(((pred - true) ** 2).mean())


def _obj_fn(params, nodes, replicas, local_bsz, step_time, step_time_compute):
    params = Params(*params)
    log_pred_step_time, pred_step_time_compute, _ = \
        _predict_log(params, nodes, replicas, local_bsz)
    # Error of total step time predictions.
    err1 = _rmse(log_pred_step_time, np.log(step_time))
    # Error of compute time predictions.
    err2 = _rmse(np.log(pred_step_time_compute), np.log(step_time_compute))
    # L2 regularization towards a smaller gamma, because it's easier to
    # optimize the alpha and beta parameters when gamma is smaller.
    reg1 = 1e-3 * (params.gamma - 1) ** 2
    # Penalize retrogression terms to prefer a more optimistic model.
    reg2 = 1e-2 * ((params.beta_n / params.alpha_n) ** 2 +
                   (params.beta_r / params.alpha_r) ** 2)
    return err1 + err2 + reg1 + reg2
