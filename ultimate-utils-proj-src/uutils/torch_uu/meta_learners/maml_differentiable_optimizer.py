import gc
from argparse import Namespace
from collections import OrderedDict
from typing import Optional

import higher
from higher.optim import _add
from higher.optim import DifferentiableOptimizer
from higher.optim import _GroupedGradsType

import torch
from higher.patch import _MonkeyPatchBase
from torch import nn
from torch import optim
from torch import Tensor
from torch.optim.optimizer import required

from anatome.helper import LayerIdentifier, dist_data_set_per_layer

FuncModel = _MonkeyPatchBase

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class NonDiffMAML(optim.Optimizer):  # copy pasted from torch.optim.SGD

    def __init__(self, params, lr=required, momentum=0, dampening=0,
                 weight_decay=0, nesterov=False):
        if lr is not required and lr < 0.0:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if momentum < 0.0:
            raise ValueError("Invalid momentum value: {}".format(momentum))
        if weight_decay < 0.0:
            raise ValueError("Invalid weight_decay value: {}".format(weight_decay))

        defaults = dict(lr=lr, momentum=momentum, dampening=dampening,
                        weight_decay=weight_decay, nesterov=nesterov)

        if nesterov and (momentum <= 0 or dampening != 0):
            raise ValueError("Nesterov momentum requires a momentum and zero dampening")
        super().__init__(params, defaults)


class MAML(DifferentiableOptimizer):  # copy pasted from DifferentiableSGD but with the g.detach() line of code

    def _update(self, grouped_grads: _GroupedGradsType, **kwargs) -> None:
        zipped = zip(self.param_groups, grouped_grads)
        for group_idx, (group, grads) in enumerate(zipped):
            weight_decay = group['weight_decay']
            momentum = group['momentum']
            dampening = group['dampening']
            nesterov = group['nesterov']

            for p_idx, (p, g) in enumerate(zip(group['params'], grads)):
                if g is None:
                    continue

                if weight_decay != 0:
                    g = _add(g, weight_decay, p)
                if momentum != 0:
                    param_state = self.state[group_idx][p_idx]
                    if 'momentum_buffer' not in param_state:
                        buf = param_state['momentum_buffer'] = g
                    else:
                        buf = param_state['momentum_buffer']
                        buf = _add(buf.mul(momentum), 1 - dampening, g)
                        param_state['momentum_buffer'] = buf
                    if nesterov:
                        g = _add(g, momentum, buf)
                    else:
                        g = buf

                if self.fo:  # first-order
                    g = g.detach()  # dissallows flow of higher order grad while still letting params track gradients.
                group['params'][p_idx] = _add(p, -group['lr'], g)


higher.register_optim(NonDiffMAML, MAML)


def get_maml_inner_optimizer(model: nn.Module, lr_inner: float) -> NonDiffMAML:
    """
    This is meant to return the non-differentiable version and once you give it to the
    get_diff optimizer (or context loop), makes it differentiable. It's a higher detail.
    """
    # inner_opt = torch_uu.optim.SGD(self.base_model.parameters(), lr=self.lr_inner)
    inner_opt = NonDiffMAML(model.parameters(), lr=lr_inner)
    # inner_opt = torch_uu.optim.Adam(self.base_model.parameters(), lr=self.lr_inner)
    # self.args.inner_opt_name = str(inner_opt)
    return inner_opt


def get_diff_optimizer_and_functional_model(model: nn.Module,
                                            opt: optim.Optimizer,
                                            copy_initial_weights: bool,
                                            track_higher_grads: bool,
                                            override: Optional = None) \
        -> tuple[FuncModel, DifferentiableOptimizer]:
    """
    Creates a functional model (for higher) and differentiable optimizer (for higher).
    Replaces higher's context manager to return a differentiable optimizer and functional model:
            with higher.innerloop_ctx(base_model, inner_opt, copy_initial_weights=args.copy_initial_weights,
                                       track_higher_grads=args.track_higher_grads) as (fmodel, diffopt):

    ref:
        - https://github.com/facebookresearch/higher/blob/main/higher/__init__.py
        - https://stackoverflow.com/questions/60311183/what-does-the-copy-initial-weights-documentation-mean-in-the-higher-library-for
        - https://github.com/facebookresearch/higher/issues/119

    :param model:
    :param opt:
    :param copy_initial_weights: DONT PUT TRUE. details: set to True only if you do NOT want to train base model's
        initialization https://stackoverflow.com/questions/60311183/what-does-the-copy-initial-weights-documentation-mean-in-the-higher-library-for
    :param track_higher_grads: set to false during meta-testing but code sets it automatically only for meta-test
    :param override:
    :return:
    """
    from higher import monkeypatch
    from higher.patch import _MonkeyPatchBase
    from higher import optim
    from higher.optim import DifferentiableOptimizer
    # - Create a monkey-patched stateless version of a module.
    fmodel: _MonkeyPatchBase = monkeypatch(
        model,
        device,
        copy_initial_weights=copy_initial_weights,
        track_higher_grads=track_higher_grads
    )
    # - Construct/initialize a differentiable version of an existing optimizer.
    diffopt: DifferentiableOptimizer = optim.get_diff_optim(
        opt,
        model.parameters(),
        fmodel=fmodel,
        device=device,
        override=override,
        track_higher_grads=track_higher_grads
    )
    return fmodel, diffopt


def get_maml_adapted_model_with_higher(base_model: nn.Module,
                                       inner_opt: optim.Optimizer,
                                       spt_x_t: Tensor, spt_y_t: Tensor,
                                       training: bool,
                                       copy_initial_weights: bool,
                                       track_higher_grads: bool,
                                       fo: bool,
                                       nb_inner_train_steps: int,
                                       criterion: nn.Module) -> FuncModel:
    """
    Return an adaptated model using MAML using pytorch's higher lib.

    Decision of .eval() and .train():
        - when training we are doing base_model.trian() because that is what the official omniglot maml higher code is
        doing. Probably that is fine since during training even if the moel collects BN stats from different tasks, it's
        not a big deal (since it can only improve or worsen the performance but at least it does not "cheat" when reporting
        meta-test accuracy results).
        - whe meta-testing we always do .eval() to avoid task info jumping illegally from one place to another. When it
        solves a task (via spt, qry set) it only uses the BN stats from training (if it has them) or the current batch
        statistics (due to mdl.eval()).

    ref:
        - official higher maml omniglot: https://github.com/facebookresearch/higher/blob/main/examples/maml-omniglot.py
        - how to do this questioon on higher: https://github.com/facebookresearch/higher/issues/119
    """
    # - get fmodel and diffopt ready for inner adaptation
    base_model.train() if training else base_model.eval()
    fmodel, diffopt = get_diff_optimizer_and_functional_model(base_model,
                                                              inner_opt,
                                                              copy_initial_weights=copy_initial_weights,
                                                              track_higher_grads=track_higher_grads)
    # - do inner addptation using task/support set
    diffopt.fo = fo
    for i_inner in range(nb_inner_train_steps):
        # base model forward pass
        spt_logits_t = fmodel(spt_x_t)
        inner_loss = criterion(spt_logits_t, spt_y_t)
        # inner-opt update
        diffopt.step(inner_loss)
    return fmodel


# def inner_loop():
#     meta_batch_size = spt_x.size(0)
#     meta_losses, meta_accs = [], []
#     for t in range(meta_batch_size):
#         spt_x_t, spt_y_t, qry_x_t, qry_y_t = spt_x[t], spt_y[t], qry_x[t], qry_y[t]
#         # - Inner Loop Adaptation
#         with higher.innerloop_ctx(self.base_model, inner_opt, copy_initial_weights=self.args.copy_initial_weights,
#                                   track_higher_grads=self.args.track_higher_grads) as (fmodel, diffopt):
#             diffopt.fo = self.fo
#             for i_inner in range(self.args.nb_inner_train_steps):
#                 fmodel.train()
#
#                 # base/child model forward pass
#                 spt_logits_t = fmodel(spt_x_t)
#                 inner_loss = self.args.criterion(spt_logits_t, spt_y_t)
#                 # inner_train_err = calc_error(mdl=fmodel, X=S_x, Y=S_y)  # for more advanced learners like meta-lstm
#
#                 # inner-opt update
#                 diffopt.step(inner_loss)

# - comparing models with maml

def dist_batch_tasks_for_all_layer_mdl_vs_adapted_mdl(
        mdl: nn.Module,
        spt_x: Tensor, spt_y: Tensor, qry_x: Tensor, qry_y: Tensor,
        layer_names: list[str],
        inner_opt: DifferentiableOptimizer,
        fo: bool,
        nb_inner_train_steps: int,
        criterion: nn.Module,
        metric_comparison_type: str = 'pwcca',
        iters: int = 1,
        effective_neuron_type: str = 'filter',
        downsample_method: Optional[str] = None,
        downsample_size: Optional[int] = None,
        subsample_effective_num_data_method: Optional[str] = None,
        subsample_effective_num_data_param: Optional[int] = None,
        metric_as_sim_or_dist: str = 'dist',
        force_cpu: bool = False,
        training: bool = False,
        copy_initial_weights: bool = False,
        track_higher_grads: bool = False
) -> list[OrderedDict[LayerIdentifier, float]]:
    """
    :param mdl:
    :param spt_x: not as a tuple due to having to move them to gpu potentially.
    :param spt_y:
    :param qry_x:
    :param qry_y:
    :param layer_names:
    :param metric_comparison_type:
    :param iters:
    :param effective_neuron_type:
    :param downsample_method:
    :param downsample_size:
    :param subsample_effective_num_data_method:
    :param subsample_effective_num_data_param:
    :param metric_as_sim_or_dist:
    :param force_cpu:
    :param training:
    :param copy_initial_weights:
    :return:
    """
    # - [B, M, C, H, W] -> [B, L]
    L: int = len(layer_names)
    B: int = spt_x.size(0)
    dists_per_batch_per_layer = list[OrderedDict[LayerIdentifier, float]] = []
    for t in range(B):
        spt_x_t, spt_y_t, qry_x_t, qry_y_t = spt_x[t], spt_y[t], qry_x[t], qry_y[t]
        #
        adapted_mdl: FuncModel = get_maml_adapted_model_with_higher(mdl,
                                                                    inner_opt,
                                                                    spt_x_t, spt_y_t,
                                                                    training,
                                                                    copy_initial_weights,
                                                                    track_higher_grads,
                                                                    fo,
                                                                    nb_inner_train_steps,
                                                                    criterion)
        # - [M, C, H, W], [L] -> [L]
        X: Tensor = qry_x_t
        dists_per_layer: OrderedDict[LayerIdentifier, float] = dist_data_set_per_layer(mdl1=mdl,
                                                                                       mdl2=adapted_mdl,
                                                                                       X1=X,
                                                                                       X2=X,
                                                                                       layer_names1=layer_names,
                                                                                       layer_names2=layer_names,
                                                                                       metric_comparison_type=metric_comparison_type,
                                                                                       iters=iters,
                                                                                       effective_neuron_type=effective_neuron_type,
                                                                                       downsample_method=downsample_method,
                                                                                       downsample_size=downsample_size,
                                                                                       subsample_effective_num_data_method=subsample_effective_num_data_method,
                                                                                       subsample_effective_num_data_param=subsample_effective_num_data_param,
                                                                                       metric_as_sim_or_dist=metric_as_sim_or_dist,
                                                                                       force_cpu=force_cpu
                                                                                       )
        assert len(dists_per_layer) == L
        # - appending to [B, L]
        dists_per_batch_per_layer.append(dists_per_layer)
        #
        # del adapted_mdl
        # gc.collect()
    assert len(dists_per_batch_per_layer) == B
    # Invariant due to asserts: [B, L] list

    # - [B, L] distances ready!
    return dists_per_batch_per_layer
