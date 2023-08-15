from typing import List, Optional, Union, Dict, Any, Literal
import logging
import gc

from torch import nn
import deepspeed
import deepspeed.compression.helper
import torch
import torch.nn.functional as F
import bitsandbytes as bnb
import dataclasses

import api.config
import api.model
import api.utils

logger = logging.getLogger("LoRA")


@dataclasses.dataclass
class LoRA8bitConfig:
    trainable: bool
    threshold: float
    memory_efficient_backward: bool


class LinearLoRA(nn.Module):
    """Taken from DeepSpeedChat.
    """

    def __init__(
        self,
        linear_: Union[nn.Linear, bnb.nn.Linear8bitLt],
        lora_dim: int = 0,
        lora_scaling: float = 1,
        lora_dropout: float = 0,
        dtype: Optional[torch.dtype] = None,
        bnb_8bit_kwargs: Optional[Dict[str, Any]] = None,
    ):
        super(LinearLoRA, self).__init__()

        bnb_8bit_config = LoRA8bitConfig(**bnb_8bit_kwargs) if bnb_8bit_kwargs is not None else None

        # sanity checks
        if bnb_8bit_config is not None and dtype is not None:
            raise RuntimeError("Cannot specify dtype when bnb_8bit is True.")
        if lora_dim <= 0:
            raise ValueError("You are training to use LoRA, whose reduced dim should be larger than 1")

        self.weight = linear_.weight
        self.bias = linear_.bias

        rows, columns = self.weight.shape

        self.use_bnb_8bit = (bnb_8bit_config is not None)
        if bnb_8bit_config is not None:
            self.lora_right = bnb.nn.Linear8bitLt(
                lora_dim,
                columns,
                bias=False,
                has_fp16_weights=bnb_8bit_config.trainable,
                threshold=bnb_8bit_config.threshold,
                device=self.weight.device,
                memory_efficient_backward=bnb_8bit_config.memory_efficient_backward,
            )
            self.lora_left = bnb.nn.Linear8bitLt(
                rows,
                lora_dim,
                bias=False,
                has_fp16_weights=bnb_8bit_config.trainable,
                threshold=bnb_8bit_config.threshold,
                device=self.weight.device,
                memory_efficient_backward=bnb_8bit_config.memory_efficient_backward,
            )
        else:
            if dtype is None:
                dtype = self.weight.dtype
            self.lora_right = nn.Linear(lora_dim, columns, bias=False).to(dtype=dtype,
                                                                          device=self.weight.device)
            self.lora_left = nn.Linear(rows, lora_dim, bias=False).to(dtype=dtype, device=self.weight.device)

        self.lora_scaling = lora_scaling / lora_dim

        if lora_dropout > 0:
            self.lora_dropout = nn.Dropout(lora_dropout)
        else:
            self.lora_dropout = nn.Identity()

        # disable the original weight gradient
        self.weight.requires_grad = False
        # fuse LoRA to the original weight
        self.fuse_lora = False

        self.squashed = False

    def eval(self):
        if not self.squashed:
            self.lora_dropout.eval()

    def train(self, mode=True):
        if not self.squashed:
            self.lora_dropout.train(mode)

    def squash_lora(self):
        if self.squashed:
            raise RuntimeError("LoRA is already squashed.")
        self.fuse_lora_weight()
        del self.lora_left
        del self.lora_right
        del self.lora_dropout
        self.squashed = True

    def fuse_lora_weight(self):
        if not self.squashed and not self.fuse_lora:
            self.weight.data += self.lora_scaling * torch.matmul(self.lora_left.weight.t(),
                                                                 self.lora_right.weight.t())
        self.fuse_lora = True

    def unfuse_lora_weight(self):
        if not self.squashed and self.fuse_lora:
            self.weight.data -= self.lora_scaling * torch.matmul(self.lora_left.weight.t(),
                                                                 self.lora_right.weight.t())
        self.fuse_lora = False

    def forward(self, x):
        y = F.linear(x, self.weight, self.bias)
        if self.squashed or self.fuse_lora:
            return y
        if self.use_bnb_8bit:
            x = x.to(torch.float16)
        return y + self.lora_right(self.lora_left(self.lora_dropout(x))) * self.lora_scaling


def convert_linear_layer_to_lora(model: nn.Module, lora_key_to_replace: str, lora_kwargs: dict,
                                 lora_exclude_module_names: List):
    replace_name = []
    for name, module in model.named_modules():
        if lora_key_to_replace not in name:
            continue
        if any(x in name for x in lora_exclude_module_names):
            continue
        if isinstance(module, [bnb.nn.Linear8bitLt, nn.Linear]):
            replace_name.append(name)
        elif 'linear' in module.__class__.__name__.lower():
            logger.warning(
                f"Found a linear-like layer {name} that is not `nn.Linear` or `bnb.nn.Linear8bitLt`. "
                f"Class {module.__class__.__name__}. This layer will not be converted to LoRA.")

    for name in replace_name:
        module: nn.Linear = deepspeed.compression.helper.recursive_getattr(model, name)
        tmp = LinearLoRA(module, **lora_kwargs)
        deepspeed.compression.helper.recursive_setattr(model, name, tmp)
    return model


def squash_all_lora_layers(model: nn.Module):
    for name in [name for name, module in model.named_modules() if isinstance(module, LinearLoRA)]:
        module: LinearLoRA = deepspeed.compression.helper.recursive_getattr(model, name)
        module.squash_lora()
    gc.collect()
    torch.cuda.empty_cache()
    gc.collect()
    return model


def fuse_all_lora_layers(model: nn.Module):
    for name in [name for name, module in model.named_modules() if isinstance(module, LinearLoRA)]:
        module: LinearLoRA = deepspeed.compression.helper.recursive_getattr(model, name)
        module.fuse_lora_weight()
    return model


def unfuse_all_lora_layers(model: nn.Module):
    for name in [name for name, module in model.named_modules() if isinstance(module, LinearLoRA)]:
        module: LinearLoRA = deepspeed.compression.helper.recursive_getattr(model, name)
        module.unfuse_lora_weight()
    return model


def only_optimize_lora_parameters(model: nn.Module, additional_module_names_to_opt: List[str]):
    for name, param in model.named_parameters():
        requires_grad = "lora_right" in name or "lora_left" in name
        for x in additional_module_names_to_opt:
            requires_grad |= x in name
        param.requires_grad = requires_grad
    logger.debug(f"Parameter names to be optimized: "
                 f"{list(n for n, p in model.named_parameters() if p.requires_grad)}.")
    return model


def get_lora_state_dict(model: nn.Module):
    lora_names = [name for name, module in model.named_modules() if isinstance(module, LinearLoRA)]
    return {k: v for k, v in model.state_dict() if k in lora_names}


def lora_wrap_fn(cls_):

    def wrapped_cls(lora_kwargs: dict,
                    lora_key_to_replace: str,
                    lora_exclude_module_names: Optional[List[str]] = None,
                    additional_module_names_to_opt: Optional[List[str]] = None,
                    load_lora_path: Optional[str] = None,
                    lora_op_after_creation: Optional[Literal['squash', 'fuse']] = None,
                    **kwargs):
        model: api.model.Model = cls_(**kwargs)

        if additional_module_names_to_opt is None:
            additional_module_names_to_opt = []
        if lora_exclude_module_names is None:
            lora_exclude_module_names = []

        model.module = convert_linear_layer_to_lora(
            model.module,
            lora_key_to_replace,
            lora_kwargs=lora_kwargs,
            lora_exclude_module_names=lora_exclude_module_names,
        )

        if load_lora_path is not None:
            logger.info(f"Loading LoRA from {load_lora_path}")
            lora_state_dict = torch.load(load_lora_path, map_location="cpu")
            names = sorted([name for name, module in model.named_modules() if isinstance(module, LinearLoRA)])
            sd_names = sorted(lora_state_dict.keys())
            if names != sd_names:
                raise RuntimeError(f"LoRA names do not match: {names} != {sd_names}")
            model.module.load_state_dict(lora_state_dict, strict=False)

        if lora_op_after_creation is None:
            pass
        elif lora_op_after_creation == 'squash':
            model.module = squash_all_lora_layers(model.module)
        elif lora_op_after_creation == 'fuse':
            model.module = fuse_all_lora_layers(model.module)
        else:
            raise NotImplementedError(f"Unknown lora_op_after_creation: {lora_op_after_creation}")

        return model

    return wrapped_cls


existing_model_classes = api.model.ALL_MODEL_CLASSES.copy()
for k, cls_ in existing_model_classes.items():
    api.model.register_model(f"{k}_lora", lora_wrap_fn(cls_))
