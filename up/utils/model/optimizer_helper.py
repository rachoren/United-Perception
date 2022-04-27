"""
  These functions are mainly used in train_val.py
"""

# Standard Library
import copy

# Import from third library
import torch

# Import from local
from up.utils.model import optim
from ..general.log_helper import default_logger as logger
from ..general.registry_factory import OPTIMIZER_REGISTRY
from collections import defaultdict
from up.utils.model.utils import get_layer_id_for_vit


def build_cls_instance(module, cfg):
    """Build instance for given cls"""
    cls = getattr(module, cfg['type'])
    return cls(**cfg['kwargs'])


def fused_sgd_reload(self):
    """When the model weights updates, e.g. load pre-train weights, the params stored in FusedFP16SGD
    will be out-of-date, so we need to reload the new weights.
    Remember call optimizer.reload whenever model's weights updated in fp16 training!
    """
    # first, rollback master param to model params in  param_groups
    for i, param_group in enumerate(self.param_groups):
        fp16_params_this_group = copy.copy(self.fp16_groups[i])
        fp32_params_this_group = copy.copy(self.fp32_groups[i])
        fp32_from_fp16_params_this_group = copy.copy(self.fp32_from_fp16_groups[i])
        for j, param in enumerate(param_group['params']):
            if param.requires_grad:
                if len(fp32_params_this_group) > 0 and fp32_params_this_group[0] is param:
                    fp32_params_this_group.pop(0)
                else:
                    master_param = fp32_from_fp16_params_this_group.pop(0)
                    model_param = fp16_params_this_group.pop(0)
                    param_group['params'][j] = model_param
                    if master_param in self.state:
                        self.state[model_param] = self.state.pop(master_param)
    self._promote_fp16_params()


@OPTIMIZER_REGISTRY.register('base')
class BaseOptimizer(object):
    def __init__(self, cfg_optim, model):
        self.cfg_optim = cfg_optim
        self.model = model

    def get_trainable_params(self, cfg_optim=None):
        special_param_group = cfg_optim.get('special_param_group', [])
        pconfig = cfg_optim.get('pconfig', None)
        if pconfig is not None:
            return self.param_group_all(self.model, pconfig)
        # trainable_params = [{"params": []}] + [{"params": [], "lr": spg['lr']} for spg in special_param_group]
        trainable_params = [{"params": []}]
        for spg in special_param_group:
            spg.update({'params': []})
            trainable_params.append(spg)
        for n, p in self.model.named_parameters():
            if p.requires_grad:
                gid = 0
                for i, spg in enumerate(special_param_group):
                    if spg['key'] in n:
                        gid = i + 1
                        break
                trainable_params[gid]["params"].append(p)
        return trainable_params

    def param_group_all(self, model, config):
        pgroup_normal = []
        pgroup_nodecay = []
        pgroup = {'bn_w': [], 'bn_b': [], 'conv_b': [], 'linear_b': []}
        names = {'bn_w': [], 'bn_b': [], 'conv_b': [], 'linear_b': []}
        if 'conv_dw_w' in config:
            pgroup['conv_dw_w'] = []
            names['conv_dw_w'] = []
        if 'conv_dw_b' in config:
            pgroup['conv_dw_b'] = []
            names['conv_dw_b'] = []
        if 'conv_dense_w' in config:
            pgroup['conv_dense_w'] = []
            names['conv_dense_w'] = []
        if 'conv_dense_b' in config:
            pgroup['conv_dense_b'] = []
            names['conv_dense_b'] = []
        if 'linear_w' in config:
            pgroup['linear_w'] = []
            names['linear_w'] = []
        if 'ln_b' in config:
            pgroup['ln_b'] = []
        if 'ln_w' in config:
            pgroup['ln_w'] = []

        nodecay = config.get('nodecay', [])
        layer_decay = config.get('layer_decay', None)

        names_all = []
        type2num = defaultdict(lambda: 0)
        for name, m in model.named_modules():
            if isinstance(m, torch.nn.Conv2d):
                if m.bias is not None:
                    if 'conv_dw_b' in pgroup and m.groups == m.in_channels:
                        pgroup['conv_dw_b'].append(m.bias)
                        names_all.append(name + '.bias')
                        names['conv_dw_b'].append(name + '.bias')
                        type2num[m.__class__.__name__ + '.bias(dw)'] += 1
                    elif 'conv_dense_b' in pgroup and m.groups == 1:
                        pgroup['conv_dense_b'].append(m.bias)
                        names_all.append(name + '.bias')
                        names['conv_dense_b'].append(name + '.bias')
                        type2num[m.__class__.__name__ + '.bias(dense)'] += 1
                    else:
                        pgroup['conv_b'].append(m.bias)
                        names_all.append(name + '.bias')
                        names['conv_b'].append(name + '.bias')
                        type2num[m.__class__.__name__ + '.bias'] += 1
                if 'conv_dw_w' in pgroup and m.groups == m.in_channels:
                    pgroup['conv_dw_w'].append(m.weight)
                    names_all.append(name + '.weight')
                    names['conv_dw_w'].append(name + '.weight')
                    type2num[m.__class__.__name__ + '.weight(dw)'] += 1
                elif 'conv_dense_w' in pgroup and m.groups == 1:
                    pgroup['conv_dense_w'].append(m.weight)
                    names_all.append(name + '.weight')
                    names['conv_dense_w'].append(name + '.weight')
                    type2num[m.__class__.__name__ + '.weight(dense)'] += 1

            elif isinstance(m, torch.nn.Linear):
                if m.bias is not None and 'linear_b' in pgroup:
                    pgroup['linear_b'].append(m.bias)
                    names_all.append(name + '.bias')
                    names['linear_b'].append(name + '.bias')
                    type2num[m.__class__.__name__ + '.bias'] += 1
                if 'linear_w' in pgroup:
                    pgroup['linear_w'].append(m.weight)
                    names_all.append(name + '.weight')
                    names['linear_w'].append(name + '.weight')
                    type2num[m.__class__.__name__ + '.weight'] += 1
            elif (isinstance(m, torch.nn.BatchNorm2d)
                  or isinstance(m, torch.nn.BatchNorm1d)):
                if m.weight is not None and 'bn_w' in pgroup:
                    pgroup['bn_w'].append(m.weight)
                    names_all.append(name + '.weight')
                    names['bn_w'].append(name + '.weight')
                    type2num[m.__class__.__name__ + '.weight'] += 1
                if m.bias is not None and 'bn_b' in pgroup:
                    pgroup['bn_b'].append(m.bias)
                    names_all.append(name + '.bias')
                    names['bn_b'].append(name + '.bias')
                    type2num[m.__class__.__name__ + '.bias'] += 1
            elif isinstance(m, torch.nn.LayerNorm):
                if m.weight is not None and 'ln_w' in pgroup:
                    pgroup['ln_w'].append(m.weight)
                    names_all.append(name + '.weight')
                if m.bias is not None and 'ln_b' in pgroup:
                    pgroup['ln_b'].append(m.bias)
                    names_all.append(name + '.bias')

        for name, p in model.named_parameters():
            nodecay_sign = 0
            for key_nodecay in nodecay:
                if key_nodecay == 'ndim_is1':
                    if p.ndim == 1:
                        pgroup_nodecay.append(p)
                        nodecay_sign = 1
                        break
                elif key_nodecay in name:
                    pgroup_nodecay.append(p)
                    nodecay_sign = 1
                    break
            if not nodecay_sign:
                pgroup_normal.append(p)

        param_groups_dict = {}
        for p in pgroup_normal:
            param_groups_dict[p] = {}
        for p in pgroup_nodecay:
            param_groups_dict[p] = {'weight_decay': 0.0}
        # param_groups_dict = [{'params': pgroup_normal}, {'params': pgroup_nodecay, 'weight_decay': 0.0}]
        for ptype in pgroup.keys():
            if ptype in config.keys():
                for p in pgroup[ptype]:
                    param_groups_dict[p].update(dict(config[ptype]))
            else:
                for p in pgroup[ptype]:
                    param_groups_dict[p].update({})

        # Handel Layer Decay
        if layer_decay is not None:
            if layer_decay['type'] == 'vit_base':
                num_layers = model.backbone.num_layers
                layer_scales = list(layer_decay['value'] ** (num_layers - i) for i in range(num_layers + 1))
                for n, p in model.named_parameters():
                    if not p.requires_grad:
                        continue
                    layer_id = get_layer_id_for_vit(n, num_layers)
                    param_groups_dict[p].update({'lr': layer_scales[layer_id] * layer_decay['base_lr']})
            else:
                raise NotImplementedError(f"Not support for layer decay type: {layer_decay['type']}")

        # Translate param_groups_dict back to param_groups which can be used in torch.optim
        param_groups = []
        for p, config in param_groups_dict.items():
            param_groups += [{'params': p, **config}]

        return param_groups

    def get_optimizer(self, cfg_optim):
        optim_type = cfg_optim['type']
        if optim_type == 'FusedFP16SGD' or optim_type == 'FusedFP16AdamW':
            from spring import linklink
            if optim_type == 'FusedFP16SGD':
                linklink.optim.FusedFP16SGD.reload = fused_sgd_reload
            if optim_type == 'FusedFP16AdamW':
                linklink.optim.FusedFP16AdamW.reload = fused_sgd_reload
            optimizer = build_cls_instance(linklink.optim, cfg_optim)
        elif optim_type == 'LARS' or optim_type == 'LAMB':
            optimizer = build_cls_instance(optim, cfg_optim)
        else:
            optimizer = build_cls_instance(torch.optim, cfg_optim)
        logger.info('build optimizer done')
        return optimizer

    def build_optimizer(self):
        cfg_optim = copy.deepcopy(self.cfg_optim)
        cfg_optim['kwargs']['params'] = self.get_trainable_params(cfg_optim)
        return self.get_optimizer(cfg_optim)
