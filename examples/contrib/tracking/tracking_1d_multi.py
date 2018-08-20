from __future__ import absolute_import, division, print_function
import math
import argparse
import os
import pdb

import torch
import torch.nn.functional as F
from torch.distributions import constraints

import pyro
import pyro.distributions as dist
import pyro.poutine as poutine
from pyro.contrib.tracking.assignment import MarginalAssignmentPersistent
from pyro.contrib.tracking.hashing import merge_points
from pyro.infer import TraceEnum_ELBO
from pyro.optim import ClippedAdam
from pyro.optim.multi import MixedMultiOptimizer, Newton

from datagen_utils import generate_observations, get_positions
from plot_utils import plot_solution, plot_exists_prob, init_plot_utils, plot_list
from experiment_utils import args2json

pyro.enable_validation(True)
smoke_test = ('CI' in os.environ)


@poutine.broadcast
def model(args, observations):
    emission_noise_scale = pyro.param("emission_noise_scale")
    states_loc = pyro.param("states_loc")
    num_objects = states_loc.shape[0]
    num_detections = observations.shape[1]
    with pyro.iarange("objects", num_objects):
        states_loc = pyro.sample("states",
                                 dist.Normal(0., 1.).expand([2]).independent(1),
                                 obs=states_loc)
    positions = get_positions(states_loc, args.num_frames)
    assert positions.shape == (args.num_frames, states_loc.shape[0])
    with pyro.iarange("detections", num_detections):
        with pyro.iarange("time", args.num_frames):
            # The remaining continuous part is exact.
            is_observed = (observations[..., -1] > 0)
            with poutine.scale(scale=is_observed.float().detach()):
                assign = pyro.sample("assign", dist.Categorical(torch.ones(num_objects + 1)))
            assert assign.shape == (num_objects + 1, args.num_frames, num_detections)  # because parallel enumeration
            observed_positions = observations[..., 0]

            assert observed_positions.shape == (args.num_frames, num_detections)
            bogus_position = positions.new_zeros(args.num_frames, 1)
            augmented_positions = torch.cat([positions, bogus_position], -1).unsqueeze(0)

            # weird tricks because index and input must be same dimension in gather
            if augmented_positions.shape[-1] > assign.shape[-1]:
                assign = F.pad(assign, (0, augmented_positions.shape[-1] - assign.shape[-1]),
                               'constant', augmented_positions.shape[-1] - 1)
            elif augmented_positions.shape[-1] < assign.shape[-1]:
                augmented_positions = F.pad(augmented_positions,
                                            (0, assign.shape[-1] - augmented_positions.shape[-1]), 'replicate')
            augmented_positions = augmented_positions.expand_as(assign)
            predicted_positions = torch.gather(augmented_positions, -1, assign)
            if args.debug:
                pdb.set_trace()
            predicted_positions = predicted_positions[..., :observations.shape[1]]
            assert predicted_positions.shape == (num_objects + 1, args.num_frames, num_detections)
            if args.debug:
                pdb.set_trace()
            with poutine.scale(scale=is_observed.float().detach()):
                pyro.sample('observations',
                            dist.Normal(predicted_positions, emission_noise_scale),
                            obs=observed_positions)


def compute_exists_logits(states_loc, args):
    log_likelihood = exists_log_likelihood(states_loc, args)
    exists_logits = log_likelihood[:, 0] - log_likelihood[:, 1]
    return exists_logits


def exists_log_likelihood(states_loc, args):
    p_exists = min(0.9999, args.expected_num_objects / states_loc.shape[0])
    p_exists = max(0.1, p_exists)
    real_part = torch.empty(states_loc.shape[0]).fill_(math.log(p_exists))
    spurious_part = torch.empty(real_part.shape).fill_(math.log(1 - p_exists))
    return torch.stack([real_part, spurious_part], -1)


def compute_assign_logits(positions, observations, emission_noise_scale, args):
    log_likelihood = assign_log_likelihood(positions, observations, emission_noise_scale, args)
    assign_logits = log_likelihood[..., :-1] - log_likelihood[..., -1:]
    is_observed = (observations[..., -1] > 0)
    assign_logits[~is_observed] = -float('inf')
    return assign_logits


def assign_log_likelihood(positions, observations, emission_noise_scale, args):
    real_dist = dist.Normal(positions.unsqueeze(-2), emission_noise_scale)
    fake_dist = dist.Uniform(-4., 4.)
    is_observed = (observations[..., -1] > 0)
    observed_positions = observations[..., :-1]
    real_part = real_dist.log_prob(observed_positions)
    fake_part = fake_dist.log_prob(observed_positions)
    log_likelihood = torch.cat([real_part, fake_part], -1)
    log_likelihood[~is_observed] = -float('inf')
    return log_likelihood


@poutine.broadcast
def guide(args, observations):
    states_loc = pyro.param("states_loc")
    emission_noise_scale = pyro.param("emission_noise_scale")
    is_observed = (observations[..., -1] > 0)
    positions = get_positions(states_loc, args.num_frames)
    assign_logits = compute_assign_logits(positions, observations, emission_noise_scale, args)
    exists_logits = compute_exists_logits(states_loc, args)
    assignment = MarginalAssignmentPersistent(
        exists_logits, assign_logits, bp_iters=args.bp_iters, bp_momentum=args.bp_momentum)
    if args.debug:
        pdb.set_trace()
    assign_dist = assignment.assign_dist
    with poutine.scale(scale=is_observed.float().detach()):
        with pyro.iarange("detections", observations.shape[1]):
            with pyro.iarange("time", args.num_frames):
                pyro.sample("assign", assign_dist, infer={"enumerate": "parallel"})
                # assign.shape == (num_objects + 1, args.num_frames, num_detections) during inference
                # assign.shape == (args.num_frames, num_detections) in single guide call (e.g. when plotting)
    return assignment.exists_dist.probs


def init_params(args, true_states=None, true_ens=None):
    if true_states is not None:
        states_loc = pyro.param("states_loc",
                                lambda: torch.cat((true_states,
                                                   torch.index_select(true_states, 0,
                                                                      torch.randint(0, true_states.shape[0],
                                                                                    (args.max_num_objects -
                                                                                     true_states.shape[0],)).long()
                                                                      )
                                                   ), 0))
    else:
        states_loc = pyro.param("states_loc", dist.Normal(0, 1).sample((args.max_num_objects, 2)))
    if true_ens is not None:
        emission_noise_scale = pyro.param("emission_noise_scale", torch.tensor(true_ens),
                                          constraint=constraints.positive)
    else:
        emission_noise_scale = pyro.param("emission_noise_scale", torch.tensor(0.01), constraint=constraints.positive)
    return states_loc, emission_noise_scale


def track_1d_objects(args, observations, true_states=None):
    pyro.set_rng_seed(args.seed + 1)  # Use a different seed from data generation
    pyro.clear_param_store()
    init_states = dist.Normal(true_states, 0.1).sample() if args.good_init in ['states', 'both'] else None
    init_ens = dist.Normal(args.emission_noise_scale, 0.1 * args.emission_noise_scale
                           ).sample().abs() if args.good_init in ['ens', 'both'] else None
    init_params(args, init_states, init_ens)

    # Optimization
    pyro.set_rng_seed(args.seed + 1)  # Use a different seed from data generation
    losses = []
    ens = []

    elbo = TraceEnum_ELBO(max_iarange_nesting=2, strict_enumeration_warning=False)
    newton = Newton(trust_radii={'states_loc': 1})
    adam = ClippedAdam({'lr': 0.1})
    optim = MixedMultiOptimizer([(['emission_noise_scale'], adam), (['states_loc'], newton)])
    try:
        for svi_step in range(args.svi_iters):
            with poutine.trace(param_only=True) as param_capture:
                loss = elbo.differentiable_loss(model, guide, args, observations)
            params = {name: pyro.param(name).unconstrained() for name in param_capture.trace.nodes.keys()}
            optim.step(loss, params)

            ens.append(pyro.param("emission_noise_scale").item())
            losses.append(loss.item() if isinstance(loss, torch.Tensor) else loss)

            if args.merge:
                with torch.no_grad():
                    p_exists = guide(args, observations)
                    updated_states_loc = pyro.param("states_loc").clone()
                    if args.prune_threshold > 0.0:
                        updated_states_loc = updated_states_loc[p_exists > args.prune_threshold]
                    if (args.merge_radius > 0.0) and (updated_states_loc.dim() == 2):
                        updated_states_loc, _ = merge_points(updated_states_loc, args.merge_radius)
                    pyro.get_param_store().replace_param('states_loc', updated_states_loc, pyro.param("states_loc"))

            if args.debug:
                print(pyro.param("states_loc"))

            if not args.quiet:
                print('epoch {: >3d} loss = {}, emission_noise_scale = {}, number of objects = {}'.format(
                    svi_step, loss, ens[-1],
                    pyro.param("states_loc").shape[0]))
    except KeyboardInterrupt:
        print('Interrupted')

    # Pruning & merging
    with torch.no_grad():
        p_exists = guide(args, observations)
        updated_states_loc = pyro.param("states_loc")
        if args.prune_threshold > 0.0:
            updated_states_loc = updated_states_loc[p_exists > args.prune_threshold]
        if (args.merge_radius > 0.0) and (updated_states_loc.dim() == 2):
            updated_states_loc, _ = merge_points(updated_states_loc, args.merge_radius)
        pyro.get_param_store().replace_param('states_loc', updated_states_loc, pyro.param("states_loc"))
    if not args.quiet:
        print(pyro.param("states_loc"))

    return losses, ens


def demo(args):
    if isinstance(args, str):
        args = parse_args(args)

    # generate data
    pyro.set_rng_seed(args.seed)
    true_states, true_positions, observations = generate_observations(args)
    true_num_objects = len(true_states)
    max_num_detections = observations.shape[1]
    assert true_states.shape == (true_num_objects, 2)
    assert true_positions.shape == (args.num_frames, true_num_objects)
    assert observations.shape == (args.num_frames, max_num_detections, 2)
    if not args.quiet:
        print("generated {:d} detections from {:d} objects".format((observations[..., -1] > 0).long().sum(),
                                                                   true_num_objects))
        print('true_states = {}'.format(true_states))

    # initialization
    viz, full_exp_dir = init_plot_utils(args)
    if full_exp_dir is not None:
        args2json(args, os.path.join(full_exp_dir, 'config.json'))
    env = str(args)

    losses, ens = track_1d_objects(args, observations, true_states)

    # run visualizations
    if (viz is not None) or (full_exp_dir is not None):
        plot_list(losses, "Loss", viz=viz, env=env, fig_dir=full_exp_dir)
        plot_list(ens, "Emission Noise Scale", viz=viz, env=env, fig_dir=full_exp_dir)

        # Run guide once and plot final result
        with torch.no_grad():
            states_loc = pyro.param("states_loc")
            positions = get_positions(states_loc, args.num_frames)
            p_exists = guide(args, observations)
        plot_solution(observations, p_exists, positions, true_positions, args,
                      pyro.param("emission_noise_scale").item(),
                      'After inference', viz=viz, env=env, fig_dir=full_exp_dir)
        plot_exists_prob(p_exists, viz, env=env, fig_dir=full_exp_dir)

    states_loc = pyro.param("states_loc")
    positions = get_positions(states_loc, args.num_frames)
    emission_noise_scale = pyro.param("emission_noise_scale")
    return true_states, states_loc, args.emission_noise_scale, emission_noise_scale


def parse_args(*args):
    from shlex import split
    parser = argparse.ArgumentParser()
    parser.add_argument('--exp-dir', default='.', help='experiment directory to log')
    parser.add_argument('--exp-name', default=None, help='experiment name to log')
    parser.add_argument('--num-frames', default=10, type=int, help='number of frames')
    parser.add_argument('--seed', default=1, type=int, help='seed')
    parser.add_argument('--max-num-objects', default=10, type=int, help='maximum number of objects')
    parser.add_argument('--expected-num-objects', default=2.0, type=float, help='expected number of objects')
    parser.add_argument('--expected-num-spurious', default=1e-5, type=float,
                        help='expected number of false positives, if this is too small, BP will be unstable.')
    parser.add_argument('--emission-prob', default=.9999, type=float,
                        help='emission probability, if this is too large, BP will be unstable.')
    parser.add_argument('--emission-noise-scale', default=0.1, type=float,
                        help='emission noise scale, if this is too small, SVI will see flat gradients.')
    parser.add_argument('--svi-iters', default=200, type=int, help='number of SVI iterations')
    parser.add_argument('--bp-iters', default=20, type=int, help='number of BP iterations')
    parser.add_argument('--bp-momentum', default=0.5, type=float, help='BP momentum')
    parser.add_argument('--no-visdom', action="store_false", dest='visdom', default=True,
                        help='Whether plotting in visdom is desired')
    parser.add_argument('--good-init', choices=['none', 'states', 'ens', 'both'], default='none',
                        help='Init states_loc & emission_noise_scale with correct values')

    parser.add_argument('--debug', action="store_true", dest='debug', default=False,
                        help='Whether plotting in visdom is desired')
    parser.add_argument('--merge-radius', default=-1, type=float, help='merge radius')
    parser.add_argument('--prune-threshold', default=-1, type=float, help='prune threshold')
    parser.add_argument('--merge-every-step', action="store_true", dest='merge', default=False,
                        help='Merge every step or just at the end')
    parser.add_argument('-q', action="store_true", dest='quiet', default=False,
                        help='Quiet')
    if len(args):
        if isinstance(args[0], str):
            args = parser.parse_args(split(args[0]))
        elif isinstance(args[0], argparse.Namespace):
            args = args[0]
        else:
            raise ValueError("args must be string or Namespace, instead got {}".format(type(args[0])))
    else:
        args = parser.parse_args()
    if args.bp_iters < 0:
        args.bp_iters = None
        assert args.max_num_objects >= args.expected_num_objects
    return args


if __name__ == '__main__':
    args = parse_args()
    demo(args)
