from typing import List, Optional, Tuple, Type, Union
import dataclasses

import numpy as np
import torch
import torch.distributed as dist
import transformers

from base.monitor import time_mark
import base.logging as logging

logger = logging.getLogger("Data Manipulation")


@torch.no_grad()
def masked_normalization(
    x,
    mask=None,
    dim=None,
    inplace=False,
    unbiased=False,
    eps=1e-5,
    high_precision=True,
):
    """Normalize x with a mask. Typically used in advantage normalization.

    Args:
        x (torch.Tensor):
            Tensor to be normalized.
        mask (torch.Tensor, optional):
            A mask with the same shape as x. Defaults to None.
        dim (int or tuple of ints, optional):
            Dimensions to be normalized. Defaults to None.
        inplace (bool, optional):
            Whether to perform in-place operation. Defaults to False.
        eps (torch.Tensor, optional):
            Minimal denominator. Defaults to 1e-5.

    Returns:
        torch.Tensor:
            Normalized x, with the same shape as x.
    """
    dtype = torch.float64 if high_precision else torch.float32
    x = x.to(dtype)
    if not inplace:
        x = x.clone()
    if dim is None:
        dim = tuple(range(len(x.shape)))
    if mask is None:
        factor = torch.tensor(np.prod([x.shape[d] for d in dim]), dtype=dtype, device=x.device)
    else:
        mask = mask.to(dtype)
        assert len(mask.shape) == len(x.shape), (mask.shape, x.shape, dim)
        for i in range(len(x.shape)):
            if i in dim:
                assert mask.shape[i] == x.shape[i], (mask.shape, x.shape, dim)
            else:
                assert mask.shape[i] == 1, (mask.shape, x.shape, dim)
        x = x * mask
        factor = mask.sum(dim, keepdim=True)
    x_sum = x.sum(dim=dim, keepdim=True)
    x_sum_sq = x.square().sum(dim=dim, keepdim=True)
    if dist.is_initialized():
        dist.all_reduce(factor, op=dist.ReduceOp.SUM)
        dist.all_reduce(x_sum, op=dist.ReduceOp.SUM)
        dist.all_reduce(x_sum_sq, op=dist.ReduceOp.SUM)
    mean = x_sum / factor
    meansq = x_sum_sq / factor
    var = meansq - mean**2
    if unbiased:
        var *= factor / (factor - 1)
    return ((x - mean) / (var.sqrt() + eps)).float()


def get_eos_indices(
    input_ids: torch.LongTensor,
    tokenizer: transformers.PreTrainedTokenizerFast,
) -> Tuple[torch.LongTensor, torch.FloatTensor]:
    if torch.any(input_ids[:, 0] == tokenizer.eos_token_id):
        indices = (input_ids[:, 0] == tokenizer.eos_token_id).nonzero().flatten()
        bad_input_ids = input_ids[indices]
        bad_strs = tokenizer.batch_decode(bad_input_ids,
                                          skip_special_tokens=True,
                                          clean_up_tokenization_spaces=True)
        raise RuntimeError(f"Generated sequence terminates unexpectedly early: {bad_strs}")
    seq_len = input_ids.shape[1]
    eos_mask = (input_ids == tokenizer.eos_token_id).float()
    seq_no_eos_mask = (eos_mask.sum(1) == 0).float()
    eos_indices = eos_mask.argmax(1)
    eos_indices = (eos_indices * (1 - seq_no_eos_mask) + seq_no_eos_mask * (seq_len - 1)).long()
    return eos_indices, seq_no_eos_mask


# TODO: temp solution, all data going through pp models must be non-boolean tensors
# before input to pipe model, convert all data to tensors (# input of pipe model should be tensors)-> convert back to original type
# after output from pipe -> convert all data to tensors
def to_tensor(x: Union[int, bool, torch.Tensor, None]):
    device = torch.cuda.current_device()
    if isinstance(x, int) or isinstance(x, bool):
        assert x >= 0
        return torch.tensor(x, dtype=torch.long, device=device)
    elif x is None:
        return torch.tensor(-1, dtype=torch.long, device=device)
    elif torch.is_tensor(x):
        if x.dtype != torch.bool:
            return x
        else:
            # convert bool tensor to int tensor
            return x.to(dtype=torch.long)  # .to(dtype=torch.long, device=device)
    else:
        raise NotImplementedError(f"Cannot convert {x} to tensor")


def from_tensor(x: torch.Tensor, _type: Type):
    try:
        if int(x) < 0:
            return None
    except:
        pass
    if _type == int:
        return int(x)
    elif _type == bool:
        return bool(x)
    elif _type == torch.Tensor:
        return x
    else:
        raise NotImplementedError(f"Cannot convert tensor to {_type}")


class TensorDataclassToTupleInterface:

    def to_tuple(self):
        # the first element of the tuple is the length of the tuple
        # sometimes the tuple can be mutliple tensor dataclass instances
        device = torch.cuda.current_device()
        t = []
        for f in dataclasses.fields(self):
            v = getattr(self, f.name)
            t.append(to_tensor(v))
        t = [torch.tensor(len(t), device=device), torch.tensor(self.encode(), device=device)] + t
        return tuple(t)

    @classmethod
    def from_tuple(cls, t):
        x = cls()
        # logger.info(f"from_tuple debug:: tuple length = {len(t)}, cls={cls}")
        for i, f in enumerate(dataclasses.fields(x)):
            # logger.info(f"from_tuple debug:: f.name={f.name}, i={i}")
            setattr(x, f.name, from_tensor(t[i + 2], f.type))
        return x

    def encode(self):
        return -1


def data_list_to_tensor_tuple(data_list: List[TensorDataclassToTupleInterface]):
    rank = dist.get_rank()
    time_mark("tensor_to_tuple_start", rank)
    res = []
    for data in data_list:
        res += list(data.to_tuple())
    time_mark("tensor_to_tuple_end", rank)
    return tuple(res)


def tensor_tuple_to_data_list(tensor_tuple: tuple):
    rank = dist.get_rank()
    time_mark("tuple_to_tensor_start", rank)
    res = []
    i = 0
    while i < len(tensor_tuple):
        num_fields = tensor_tuple[i]
        type_code = int(tensor_tuple[i + 1])
        if type_code == 0:
            cls_ = PipeTransferData
        elif type_code == 1:
            cls_ = PipeCacheData
        else:
            raise NotImplementedError(f"Unknown type code {type_code}")
        res.append(cls_.from_tuple(tensor_tuple[i:i + num_fields + 2]))
        i += num_fields + 2
    time_mark("tuple_to_tensor_end", rank)
    return res


@dataclasses.dataclass
class PipeTransferData(TensorDataclassToTupleInterface):
    """Data structure for transferring data between stages.

    Each pipeline stage has exactly one PipeTransferData as the input and the output,
    no matter how many layers are in this stage.

    Attributes:
        pp_input: The input to the current stage. Usually hidden states
            with shape [bs, seq_len, hidden_dim].
        pp_output: The output of the current stage, also the input to the next stage.
            Usually hidden states with shape [bs, seq_len, hidden_dim].
        cu_seqlens: The cumulative sequence lengths of packed input_ids.
            Used by flash_attn_varlen_func. Will not be used during generation.
            It's configuration-like data that must be transfered from the first stage
            to the last. Shape [bs + 1].
        max_seqlen: The maximum sequence length of packed input_ids.
            Used by flash_attn_varlen_func. Will not be used during generation.
            It's configuration-like data that must be transfered from the first stage
            to the last.
        store_kvcache: Whether to store the key and value cache for generation.
        attention_mask: The attention mask of the input, the same as huggingface transformers.
            Used by torch_attn_func to examine the outputs of PyTorch attention and flash
            attention are the same. Only for debugging. Shape [bs, seq_len].
    """

    pp_input: torch.Tensor = None
    pp_output: torch.Tensor = None

    # The followings are "configuration"-like data that should be passed across all stages.
    cu_seqlens: torch.Tensor = None
    max_seqlen: int = None
    store_kv_cache: bool = False

    # Only used for debugging
    attention_mask: torch.Tensor = None

    def encode(self):
        return 0


@dataclasses.dataclass
class PipeCacheData(TensorDataclassToTupleInterface):
    """Data structure for caching data locally that will not be trasferred.

    Each layer has exactly one PipeCacheData as the input.
    If a pipeline stage has multiple layers, a list of PipeCacheData should be passed
    as the input. The cached tensors will be changed in-place.

    Attributes:
        input_ids: The input token ids. Used only at the first stage.
            Can be packed with shape [total_seq_len] or unpacked with shape [bs, seq].
        prompt_mask: Prompt mask used
        position_ids: Input position IDs. Can be resolved automatically in most cases.
            Used only at the first stage. The same shape as input_ids.
            If None, will be resolved automatically.
        k_cache: Key cache used for generation, shape [bs, max_seq, n_kv_heads, head_dim].
            Note that this is the cache for a specific layer, not for all layers.
        v_cache: Value cache used for generation, shape [bs, max_seq, n_kv_heads, head_dim].
            Note that this is the cache for a specific layer, not for all layers.
        cache_seqlens: The sequence lengths of the cached tokens. Used for generation. Shape [bs].
    """

    # Only cached in the first stage.
    input_ids: torch.Tensor = None
    position_ids: torch.Tensor = None
    # Cached in each transformer layer.
    k_cache: torch.Tensor = None
    v_cache: torch.Tensor = None
    cache_seqlens: torch.Tensor = None

    def encode(self):
        return 1


@dataclasses.dataclass
class DuckModelOutput:
    logits: Optional[Union[List[torch.Tensor], torch.Tensor]] = None


@dataclasses.dataclass
class DuckGenerationOutput:
    sequences: torch.Tensor
    scores: Optional[torch.Tensor] = None
    logits_mask: Optional[torch.Tensor] = None


@torch.jit.script
def upcast_masked_softmax(x: torch.Tensor, mask: torch.Tensor, mask_value: torch.Tensor, scale: float,
                          softmax_dtype: torch.dtype):
    input_dtype = x.dtype
    x = x.to(softmax_dtype) * scale
    x = torch.where(mask, x, mask_value)
    x = torch.nn.functional.softmax(x, dim=-1).to(input_dtype)
    return x


@torch.jit.script
def upcast_softmax(x: torch.Tensor, scale: float, softmax_dtype: torch.dtype):
    input_dtype = x.dtype
    x = x.to(softmax_dtype) * scale
    x = torch.nn.functional.softmax(x, dim=-1).to(input_dtype)
    return x


@torch.jit.script
def masked_softmax(x: torch.Tensor, mask: torch.Tensor, mask_value: torch.Tensor):
    x = torch.where(mask, x, mask_value)
    x = torch.nn.functional.softmax(x, dim=-1)
    return x


def repeat_kv(x: torch.Tensor, n_rep: int) -> torch.Tensor:
    """torch.repeat_interleave(x, dim=2, repeats=n_rep)"""
    bs, slen, n_kv_heads, head_dim = x.shape
    if n_rep == 1:
        return x
    return (x[:, :, :, None, :].expand(bs, slen, n_kv_heads, n_rep,
                                       head_dim).reshape(bs, slen, n_kv_heads * n_rep, head_dim))


def mask_eos_token(
    logits: torch.Tensor,
    eos_token_id: Optional[int] = None,
) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:
    # for min_new_tokens
    if eos_token_id is not None:
        logits[..., eos_token_id] = torch.finfo(logits.dtype).min
    return logits


def gather_shifted_log_probs(logits: torch.FloatTensor, labels: torch.LongTensor) -> torch.FloatTensor:
    """Gather log probs of shifted labels from logits.

    Args:
        logits (torch.FloatTensor): Non-shifted logits with shape [bs, seqlen].
            The final value at [:, seqlen -1] is not used.
        labels (torch.LongTensor): Non-shifted labels/input_ids with shape [bs, seqlen].
            The first value at [:, 0] has no corresponding log prob.

    Returns:
        torch.FloatTensor: Shifted log probability with shape [bs, seqlen -1].
    """
    logits = logits[:, :-1]
    labels = labels[:, 1:]
    log_probs = torch.nn.functional.log_softmax(logits, dim=-1)
    log_probs_labels = log_probs.gather(dim=-1, index=labels.unsqueeze(-1))
    return log_probs_labels.squeeze(-1)


def gather_packed_shifted_log_probs(logits_: torch.FloatTensor, cu_seqlens: torch.Tensor,
                                    labels_: torch.LongTensor) -> torch.FloatTensor:
    """Gather log probs from packed input_ids and logits.

    Args:
        logits_ (torch.FloatTensor): Shape [tot_seqlen]. The final value at the end of
            each sequence is not used.
        cu_seqlens (torch.Tensor): Shape [#seqs + 1]. Indices marking the start
            and end of each sequences.
        labels_ (torch.LongTensor): Labels or input_ids with shape [tot_seqlen].
            The first value at the beginning of each sequence has no corresponding log prob.

    Returns:
        torch.FloatTensor: Log probability with shape [tot_seqlen - #seqs].
    """
    leave_one_indices = torch.cat([
        torch.arange(cu_seqlens[i], cu_seqlens[i + 1] - 1, dtype=torch.long, device=cu_seqlens.device)
        for i in range(cu_seqlens.shape[0] - 1)
    ])
    shift_one_indices = torch.cat([
        torch.arange(cu_seqlens[i] + 1, cu_seqlens[i + 1], dtype=torch.long, device=cu_seqlens.device)
        for i in range(cu_seqlens.shape[0] - 1)
    ])
    logits = logits_[leave_one_indices]
    labels = labels_[shift_one_indices]
    log_probs = torch.nn.functional.log_softmax(logits, dim=-1)
    log_probs_labels = log_probs.gather(dim=-1, index=labels.unsqueeze(-1)).squeeze(-1)
    assert log_probs_labels.shape[0] == logits_.shape[0] - cu_seqlens.shape[0] + 1, (
        log_probs_labels.shape,
        logits_.shape,
        cu_seqlens.shape,
        cu_seqlens,
        shift_one_indices,
    )
    return log_probs_labels
