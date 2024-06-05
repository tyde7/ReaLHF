from typing import Dict, Optional, Tuple
import collections
import dataclasses
import itertools
import time

from deepspeed import DeepSpeedEngine
import torch
import torch.distributed as dist

from reallm.api.core import data_api
from reallm.base.monitor import cuda_tmark, cuda_tmarked, CUDATimeMarkType
from reallm.base.namedarray import from_dict, NamedArray, recursive_apply
from reallm.impl.model.backend.pipe_engine.ds_pipe_engine import (PipelinableModelRunner,
                                                                  PipelinableModelRunnerWithZeRO)
from reallm.impl.model.nn.real_llm_api import ReaLModel
from reallm.impl.model.nn.real_llm_generate import generate, GenerationConfig
from reallm.impl.model.utils.functional import gather_packed_shifted_log_probs, masked_normalization
from reallm.impl.model.utils.padding import unpad_input
import reallm.api.core.model_api as model_api
import reallm.base.constants as constants
import reallm.base.logging as logging
import reallm.impl.model.utils.ppo_functional as ppo_functional

logger = logging.getLogger("PackedPPOInterface")


def _ppo_actor_loss_from_model_outputs(
    logits: torch.FloatTensor,  # [tot_seqlen, vocab_size]
    packed_input_ids: torch.LongTensor,  # [tot_seqlen]
    cu_seqlens: torch.LongTensor,  # [bs+1]
    old_logp: torch.FloatTensor,  # [tot_seqlen-bs]
    ppo_loss_mask: torch.FloatTensor,  # [tot_seqlen-bs]
    advantages: torch.FloatTensor,  # [tot_seqlen-bs]
    kl_rewards: torch.FloatTensor,  # [tot_seqlen-bs]
    kl_adapter: ppo_functional.KLController,  # const
    eps_clip: int,  # const
    early_stop_imp_ratio: Optional[float],  # const
    early_stop_kl: Optional[float],  # const
    logits_mask: Optional[torch.BoolTensor] = None,  # [tot_seqlen, vocab_size]
    **kwargs,
) -> Tuple[torch.FloatTensor, Dict]:
    """Loss function for ppo actor step, all inputs should be splitted into pipeline micro batches,
    returns loss and logging stats.
    """
    if logits_mask is not None:
        # inplace operation for logits mask
        logits.masked_fill_(logits_mask.logical_not_(), torch.finfo(logits.dtype).min)

    n_tokens = ppo_loss_mask.count_nonzero()
    logprobs = gather_packed_shifted_log_probs(logits, cu_seqlens, packed_input_ids).float()
    loss, ppo_stat = ppo_functional.actor_loss_fn(
        logprobs=logprobs,
        old_logprobs=old_logp,
        advantages=advantages,
        eps_clip=eps_clip,
        loss_mask=ppo_loss_mask,
    )

    # FIXME: The memory efficient loss function is buggy. It does not produce gradients correctly.
    # assert ppo_loss_mask is not None
    # (loss, importance_weight, clip_ratio, approx_kl) = (ppo_functional.memory_efficient_ppo_loss_fn(
    #     logits=logits,
    #     cu_seqlens=cu_seqlens,
    #     packed_input_ids=packed_input_ids,
    #     ppo_loss_mask=ppo_loss_mask,
    #     old_logprobs=old_logp,
    #     advantages=advantages,
    #     eps_clip=eps_clip,
    # ))
    # loss = torch.where(ppo_loss_mask, loss, 0.0).sum() / ppo_loss_mask.count_nonzero()
    importance_weight = ppo_stat["importance_weight"] * n_tokens
    clip_ratio = ppo_stat["clip_ratio"] * n_tokens
    approx_kl = ppo_stat["approx_kl"] * n_tokens

    # Logging and early stopping according to KL (logp vs ref) or importance ratio (new logp vs old logp).
    mean_ref_kl = (kl_rewards.detach() * ppo_loss_mask).sum()
    logging_loss = torch.where(ppo_loss_mask, loss.detach(), 0.0).sum()
    dist.all_reduce(n_tokens, group=constants.data_parallel_group())
    dist.all_reduce(mean_ref_kl, group=constants.data_parallel_group())
    dist.all_reduce(importance_weight, group=constants.data_parallel_group())
    dist.all_reduce(clip_ratio, group=constants.data_parallel_group())
    dist.all_reduce(approx_kl, group=constants.data_parallel_group())
    dist.all_reduce(logging_loss, group=constants.data_parallel_group())

    # Early stopping.
    kl_adapter.update(mean_ref_kl / n_tokens, n_steps=cu_seqlens.shape[0] - 1)
    _imp = importance_weight / n_tokens
    _kl = approx_kl / n_tokens
    if early_stop_imp_ratio is not None and _imp > early_stop_imp_ratio:
        logger.warning(f"Current importance ratio {_imp.item():.4f} is larger "
                       f"than early stop threshold {early_stop_imp_ratio}. Abandon this minibatch.")
        loss = loss * 0.0
    if early_stop_kl is not None and _kl > early_stop_kl:
        logger.warning(f"Current approximate KL divergence {_kl.item():.4f} is larger "
                       f"than early stop threshold {early_stop_kl}. Abort actor update.")
        loss = loss * 0.0

    stats = dict(
        ppo_approx_kl=approx_kl,
        actor_loss=logging_loss,
        actor_clip_ratio=clip_ratio,
        importance_weight=importance_weight,
    )

    if logits_mask is not None:
        n_valid_vocabs = logits_mask.count_nonzero()
        total_vocabs = logits_mask.numel()
        dist.all_reduce(n_valid_vocabs, group=constants.data_parallel_group())
        dist.all_reduce(total_vocabs, group=constants.data_parallel_group())
        stats["n_valid_vocabs"] = n_valid_vocabs
        stats["total_vocabs"] = total_vocabs

    return loss, stats


@dataclasses.dataclass
class PPOActorInterface(model_api.ModelInterface):
    n_minibatches: int = 4

    generation_config: Optional[Dict] = None

    kl_ctl: float = 0.1

    adv_norm: bool = True
    discount: float = 1.0
    gae_lambda: float = 1.0

    eps_clip: float = 0.2
    value_eps_clip: float = 0.2
    max_reward_clip: float = 5.0

    early_stop_kl: Optional[float] = None  # e.g. 0.1
    early_stop_imp_ratio: Optional[float] = None  # e.g., 10.0

    adaptive_kl_ctl: bool = False
    adaptive_kl_target: Optional[float] = 6
    adaptive_kl_horizon: Optional[float] = 10000

    enable_save: bool = True
    force_no_logits_mask: bool = False

    value_norm: bool = False
    value_norm_type: str = dataclasses.field(metadata={"choices": ["exp", "ma"]}, default="exp")
    value_norm_beta: float = 0.99995
    value_norm_eps: float = 1e-5

    def __post_init__(self):
        super().__post_init__()
        if self.adaptive_kl_ctl:
            assert self.adaptive_kl_target is not None
            assert self.adaptive_kl_horizon is not None
            self.kl_adapter = ppo_functional.AdaptiveKLController(self.kl_ctl, self.adaptive_kl_target,
                                                                  self.adaptive_kl_horizon)
        else:
            self.kl_adapter = ppo_functional.FixedKLController(self.kl_ctl)
        if self.value_norm:
            from reallm.impl.model.modules import ExponentialRunningMeanStd, MovingAverageRunningMeanStd

            if self.value_norm_type == "exp":
                self.rms = ExponentialRunningMeanStd(beta=self.value_norm_beta, epsilon=self.value_norm_eps)
            elif self.value_norm_type == "ma":
                self.rms = MovingAverageRunningMeanStd()
            else:
                raise ValueError(f"Unknown value_norm_type {self.value_norm_type}")
        self.kl_ctl = None

        self._last_gen_sd = None

    def save(self, model: model_api.Model, save_dir: str):
        if not self.enable_save:
            return
        module = model.module
        if not isinstance(module, ReaLModel):
            module = module.module
        module.save_to_hf(
            tokenizer=model.tokenizer,
            save_dir=save_dir,
        )

    @torch.no_grad()
    def generate(self, model: model_api.Model, data: NamedArray) -> NamedArray:
        module = model.module

        module.eval()

        data = recursive_apply(data, lambda x: x.to(model.device))
        packed_prompts = data["packed_prompts"]
        prompt_lengths = torch.tensor(data.metadata["seqlens"],
                                      dtype=torch.int32,
                                      device=packed_prompts.device)
        cu_seqlens = torch.nn.functional.pad(prompt_lengths.cumsum(0), (1, 0))

        bs = prompt_lengths.shape[0]

        # logger.info(f"packed_prompts shape {packed_prompts.shape}, bs {bs}")

        sd = {k: v.detach().clone() for k, v in module.state_dict().items()}
        if self._last_gen_sd is not None:
            param_changed = False
            changed_keys = []
            for k, v1, v2 in zip(sd.keys(), sd.values(), self._last_gen_sd.values()):
                if not torch.allclose(v1, v2):
                    param_changed = True
                    changed_keys.append(k)
            print(">>>>>>>> actor gen param changed?", param_changed)
        self._last_gen_sd = sd      
        # st = time.monotonic()
        if isinstance(module, (PipelinableModelRunner, PipelinableModelRunnerWithZeRO)):
            res = module.generate(
                seqlens_cpu=data.metadata["seqlens"],
                tokenizer=model.tokenizer,
                packed_input_ids=packed_prompts,
                cu_seqlens=cu_seqlens,
                gconfig=GenerationConfig(**self.generation_config),
            )
            if res is None:
                return None

            gen_tokens, logprobs, logits_mask, *_ = res
            # logger.info(f"gen_tokens shape {gen_tokens.shape}")
        else:
            # unwrap deepspeed engine here
            if hasattr(module, "module"):
                module = module.module
            gen_res = module.generate(
                tokenizer=model.tokenizer,
                packed_input_ids=packed_prompts,
                cu_seqlens=cu_seqlens,
                max_seqlen=int(max(prompt_lengths)),
                gconfig=GenerationConfig(**self.generation_config),
            )
            gen_tokens = gen_res.sequences
            logprobs = gen_res.scores
            logits_mask = gen_res.logits_mask

        pad_token_id = model.tokenizer.pad_token_id
        eos_token_id = model.tokenizer.eos_token_id
        seq_no_eos_mask = (gen_tokens[:, -1] != eos_token_id).logical_and(gen_tokens[:, -1] != pad_token_id)
        # We also want gen_lengths to include the eos token, where the reward model outputs a score for this sequence.
        gen_lengths = (gen_tokens != pad_token_id).logical_and(gen_tokens != eos_token_id).sum(dim=-1) + 1
        gen_lengths = gen_lengths.clip(max=gen_tokens.shape[-1])

        # TODO: refactor the following whole bunch of sh*t.
        # Pack generated sequences and logprobs.
        prompts_list, prompt_log_probs_list, prompt_logits_mask_list = [], [], []
        gen_tokens_list, gen_log_probs_list, gen_logits_mask_list = [], [], []
        for i in range(bs):
            prompt_len, gen_len = prompt_lengths[i].item(), gen_lengths[i].item()

            # Prompts are left-padded. Besides, prompt_log_probs is one-step shorter than prompts.
            prompts_list.append(packed_prompts[cu_seqlens[i]:cu_seqlens[i + 1]])
            prompt_log_probs_list.append(logprobs.new_zeros(prompt_len - 1))
            if logits_mask is not None:
                prompt_logits_mask_list.append(logits_mask.new_ones((prompt_len - 1, logits_mask.shape[-1])))

            # Generated tokens are right-padded.
            gen_tokens_list.append(gen_tokens[i, :gen_len])
            gen_log_probs_list.append(logprobs[i, :gen_len])
            if logits_mask is not None:
                gen_logits_mask_list.append(
                    torch.cat([
                        logits_mask[i, :gen_len],
                        logits_mask.new_ones(1, logits_mask.shape[-1]),
                    ]))

        # For complete sequences, EOS token is included. Otherwise the sequence may end with arbitrary token.
        # cu_seqlens marks the boundary of these sequences, no matter whether they are complete or not.
        packed_seq = torch.cat(list(itertools.chain.from_iterable(zip(prompts_list, gen_tokens_list))))
        seq_lengths = prompt_lengths + gen_lengths
        cu_seqlens = torch.cat([
            torch.zeros(1, dtype=torch.long, device=seq_lengths.device),
            seq_lengths.cumsum(0),
        ]).int()
        packed_logprobs = torch.cat(
            list(itertools.chain.from_iterable(zip(prompt_log_probs_list, gen_log_probs_list))))
        assert packed_seq.shape[0] == packed_logprobs.shape[0] + bs, (
            packed_seq.shape,
            packed_logprobs.shape,
            bs,
        )
        packed_logits_mask = None
        if gen_logits_mask_list and not self.force_no_logits_mask:
            packed_logits_mask = torch.cat(
                list(itertools.chain.from_iterable(zip(prompt_logits_mask_list, gen_logits_mask_list))))

        prompt_mask = zip(
            [torch.ones(plen, dtype=torch.bool, device=model.device) for plen in prompt_lengths],
            [torch.zeros(glen, dtype=torch.bool, device=model.device) for glen in gen_lengths],
        )
        prompt_mask = torch.cat(list(itertools.chain.from_iterable(prompt_mask)))

        res = dict(
            seq_no_eos_mask=seq_no_eos_mask,
            packed_seq=packed_seq,
            cu_seqlens=cu_seqlens,
            packed_logprobs=packed_logprobs,
            packed_logits_mask=(packed_logits_mask.bool() if packed_logits_mask is not None else None),
            prompt_mask=prompt_mask,
        )
        res = from_dict(res)
        seqlens = seq_lengths.cpu().numpy().tolist()
        res.register_metadata(seqlens=seqlens)
        return res

    @torch.no_grad()
    def inference(self, model: model_api.Model, data: NamedArray) -> NamedArray:
        module = model.module
        module.eval()
        data = recursive_apply(data, lambda x: x.to(model.device))

        cu_seqlens = data["cu_seqlens"].int()
        input_lens = cu_seqlens[1:] - cu_seqlens[:-1]
        max_seqlen = int(max(input_lens))

        if isinstance(module, (PipelinableModelRunner, PipelinableModelRunnerWithZeRO)):
            res = module.forward(
                seqlens_cpu=data.metadata["seqlens"],
                packed_input_ids=data["packed_seq"],
                cu_seqlens=cu_seqlens,
            )
            if res is None:
                return None
            logits = res
        else:
            if hasattr(module, "module"):
                module = module.module
            res = module(
                packed_input_ids=data["packed_seq"],
                cu_seqlens=cu_seqlens,
                max_seqlen=max_seqlen,
            )
            logits = res.logits

        if "packed_logits_mask" in data and data["packed_logits_mask"] is not None:
            packed_logits_mask = data["packed_logits_mask"]
            if constants.model_parallel_world_size() > 1:
                from reallm.impl.model.parallelism.model_parallel.mappings import \
                    gather_from_tensor_model_parallel_region

                logits = gather_from_tensor_model_parallel_region(logits)
            logits.masked_fill_(packed_logits_mask.logical_not_(), torch.finfo(logits.dtype).min)
        # FIXME: the following line may OOM
        logprobs = gather_packed_shifted_log_probs(logits, cu_seqlens, data["packed_seq"])
        res = from_dict(dict(logprobs=logprobs))
        res.register_metadata(seqlens=data.metadata["seqlens"])
        return res

    def train_step(self, model: model_api.Model, data_: NamedArray) -> Dict:
        module = model.module
        tokenizer = model.tokenizer
        # We call module.eval() because dropout causes the computation of incorrect of log probs.
        module.eval()
        data_ = recursive_apply(data_, lambda x: x.to(model.device))

        old_logp: torch.FloatTensor = data_["packed_logprobs"].float()
        ref_logp: torch.FloatTensor = data_["packed_ref_logprobs"].float()
        prompt_mask = data_["prompt_mask"]
        cu_seqlens = data_["cu_seqlens"].int()
        reward_score = data_["rewards"].float()
        values = data_["values"].float()
        seq_no_eos_mask = data_["seq_no_eos_mask"]

        if self.value_norm:
            denormalized_values = self.rms.denormalize(values)
        else:
            denormalized_values = values

        for i in range(seq_no_eos_mask.shape[0]):
            if not seq_no_eos_mask[i]:
                # Set value at the EOS token to be zero.
                denormalized_values[cu_seqlens[i + 1] - 1] = 0.0
                values[cu_seqlens[i + 1] - 1] = 0.0

        # Shift the loss mask by one token for each packed sequences.
        input_lens = cu_seqlens[1:] - cu_seqlens[:-1]
        short1cu_seqlens = cu_seqlens.clone()
        short1cu_seqlens[1:] -= torch.ones_like(cu_seqlens[1:]).cumsum(0)
        loss_mask = prompt_mask.logical_not()
        shift_one_indices = torch.cat([
            torch.arange(
                cu_seqlens[i] + 1,
                cu_seqlens[i + 1],
                dtype=torch.long,
                device=cu_seqlens.device,
            ) for i in range(cu_seqlens.shape[0] - 1)
        ])
        loss_mask = loss_mask[shift_one_indices]

        # Apply the mask to log probabilities.
        ref_logp *= loss_mask
        old_logp *= loss_mask

        # Compute rewards and GAEs.
        kl_rewards, rewards = ppo_functional.get_packed_rewards(
            kl_ctl=self.kl_adapter.value,
            clip_reward_value=self.max_reward_clip,
            log_probs=old_logp,
            ref_log_probs=ref_logp,
            reward_score=reward_score,
            short1cu_seqlens=short1cu_seqlens,
            seq_no_eos_mask=seq_no_eos_mask,
        )
        advantages, returns = ppo_functional.get_packed_advantages_and_returns(
            gamma=self.discount,
            lam=self.gae_lambda,
            values=denormalized_values,
            rewards=rewards,
            short1cu_seqlens=short1cu_seqlens,
            seq_no_eos_mask=seq_no_eos_mask,
        )

        # Optionally perform normalization.
        if self.value_norm:
            self.rms.update(returns, mask=loss_mask)
        if self.adv_norm:
            advantages = masked_normalization(advantages, loss_mask)

        # Prepare data to be splitted into mini-batches.
        batch_seqlens = data_.metadata["seqlens"]
        data_ = from_dict(
            dict(
                advantages=advantages,
                old_logp=old_logp,
                ppo_loss_mask=loss_mask,
                packed_seq=data_["packed_seq"],
                cu_seqlens=data_["cu_seqlens"].int(),
                kl_rewards=kl_rewards,
                logits_mask=(data_["packed_logits_mask"] if "packed_logits_mask" in data_ else None),
            ))
        data_.register_metadata(seqlens=batch_seqlens)
        datas = data_api.split_sequences(data_,
                                         self.n_minibatches,
                                         min_size=constants.pipe_parallel_world_size() * 2)

        ### Logging code starts. ###
        _n_seqs = torch.tensor([reward_score.shape[0]], dtype=torch.float32, device=model.device)
        _n_tokens = loss_mask.count_nonzero()
        task_reward = reward_score.sum()
        _advantages = advantages.sum()
        _kl_rewards = (kl_rewards * loss_mask).sum()
        prompt_len = prompt_mask.count_nonzero().float()
        seq_len = (cu_seqlens[1:] - cu_seqlens[:-1]).float().sum()
        dist.all_reduce(_n_seqs, group=constants.data_parallel_group())
        dist.all_reduce(task_reward, group=constants.data_parallel_group())
        dist.all_reduce(_advantages, group=constants.data_parallel_group())
        dist.all_reduce(prompt_len, group=constants.data_parallel_group())
        dist.all_reduce(seq_len, group=constants.data_parallel_group())
        dist.all_reduce(_n_tokens, group=constants.data_parallel_group())
        dist.all_reduce(_kl_rewards, group=constants.data_parallel_group())

        global_stats = dict(
            task_reward=float(task_reward / _n_seqs),
            kl_reward=float(_kl_rewards / _n_tokens),
            advantage=float(_advantages / _n_tokens),
            avg_seq_len=float(seq_len / _n_seqs),
            avg_prompt_len=float(prompt_len / _n_seqs),
            n_tokens=int(_n_tokens),
            n_seqs=int(_n_seqs),
        )
        ### Logging code ends. ###

        sd = {k: v.detach().clone() for k, v in module.state_dict().items()}
        # NOTE: We cannot randomly shuffle data here because
        # data must have the same shape across different pipeline stages.
        train_stats = collections.defaultdict(lambda: 0)
        offset = 0
        for data in datas:
            cu_seqlens = data["cu_seqlens"]
            input_lens = cu_seqlens[1:] - cu_seqlens[:-1]
            logits_mask = (data["packed_logits_mask"] if "packed_logits_mask" in data else None)
            if isinstance(module, (PipelinableModelRunner, PipelinableModelRunnerWithZeRO)):
                module.set_version_steps(model.version.global_step)
                seqlens = batch_seqlens[offset:offset + input_lens.shape[0]]
                offset += input_lens.shape[0]
                loss_fn_kwargs = dict(
                    input_lens=input_lens,  # used for partition
                    old_logp=data["old_logp"],
                    ppo_loss_mask=data["ppo_loss_mask"],
                    advantages=data["advantages"],
                    kl_rewards=data["kl_rewards"],
                    kl_adapter=self.kl_adapter,
                    eps_clip=self.eps_clip,
                    early_stop_imp_ratio=self.early_stop_imp_ratio,
                    early_stop_kl=self.early_stop_kl,
                    logits_mask=logits_mask,
                )

                loss, stats = module.train_batch(
                    seqlens_cpu=seqlens,
                    packed_input_ids=data["packed_seq"],
                    cu_seqlens=data["cu_seqlens"],
                    loss_fn=_ppo_actor_loss_from_model_outputs,
                    **loss_fn_kwargs,
                )
            else:
                max_seqlen = int(max(input_lens))
                output = module(
                    packed_input_ids=data["packed_seq"],
                    cu_seqlens=cu_seqlens,
                    max_seqlen=max_seqlen,
                )
                loss, stats = _ppo_actor_loss_from_model_outputs(
                    logits=output.logits,
                    packed_input_ids=data["packed_seq"],
                    cu_seqlens=cu_seqlens,
                    old_logp=data["old_logp"],
                    ppo_loss_mask=data["ppo_loss_mask"],
                    advantages=data["advantages"],
                    kl_rewards=data["kl_rewards"],
                    kl_adapter=self.kl_adapter,
                    eps_clip=self.eps_clip,
                    early_stop_imp_ratio=self.early_stop_imp_ratio,
                    early_stop_kl=self.early_stop_kl,
                    logits_mask=logits_mask,
                )

                with cuda_tmarked("bwd", CUDATimeMarkType.backward):
                    module.backward(loss)

                with cuda_tmarked("optim_step", CUDATimeMarkType.optim_step):
                    module.step(lr_kwargs={"epoch": model.version.global_step})

            if stats:
                for k, v in stats.items():
                    train_stats[k] += v

        sd2 = {k: v.detach().clone() for k, v in module.state_dict().items()}
        param_changed = False
        changed_keys = []
        for k, v1, v2 in zip(sd.keys(), sd.values(), sd2.values()):
            if not torch.allclose(v1, v2):
                param_changed = True
                changed_keys.append(k)
        print(">>>>>>>> actor train param changed?", param_changed)
        cur_epoch = model.version.epoch
        model.inc_version()

        if train_stats:
            train_stats = dict(
                ppo_approx_kl=float(train_stats["ppo_approx_kl"] / _n_tokens),
                actor_loss=float(train_stats["actor_loss"] / _n_tokens),
                actor_clip_ratio=float(train_stats["actor_clip_ratio"] / _n_tokens),
                importance_weight=float(train_stats["importance_weight"] / _n_tokens),
            )
            train_stats = dict(**train_stats, **global_stats)

        return dict(train_stats)


def _ppo_critic_loss_from_model_outputs(
    new_values: torch.FloatTensor,
    packed_input_ids: torch.LongTensor,
    cu_seqlens: torch.LongTensor,
    values: torch.FloatTensor,
    ppo_loss_mask: torch.FloatTensor,
    returns: torch.FloatTensor,
    kl_rewards: torch.FloatTensor,
    value_eps_clip: float,
    kl_adapter: ppo_functional.KLController,
    rms=None,
    **kwargs,
) -> Tuple[torch.FloatTensor, Dict]:
    leave_one_indices = torch.cat([
        torch.arange(
            cu_seqlens[i],
            cu_seqlens[i + 1] - 1,
            dtype=torch.long,
            device=cu_seqlens.device,
        ) for i in range(cu_seqlens.shape[0] - 1)
    ])
    new_values = new_values[leave_one_indices].squeeze(-1)
    values = values[leave_one_indices].squeeze(-1)

    loss, loss_stat = ppo_functional.critic_loss_fn(
        value=new_values,
        old_value=values,
        target_value=returns,
        value_eps_clip=value_eps_clip,
        loss_mask=ppo_loss_mask,
    )

    if rms is not None:
        denormalized_values = rms.denormalize(new_values)
    else:
        denormalized_values = new_values

    # Logging.
    n_tokens = ppo_loss_mask.count_nonzero()
    mean_ref_kl = (kl_rewards.detach() * ppo_loss_mask).sum()
    logging_loss = loss.detach() * n_tokens
    clip_ratio = loss_stat["clip_ratio"] * n_tokens
    normalized_values = torch.where(ppo_loss_mask, new_values, 0.0).sum().detach()
    denormalized_values = (torch.where(ppo_loss_mask, denormalized_values, 0.0).sum().detach())
    dist.all_reduce(n_tokens, group=constants.data_parallel_group())
    dist.all_reduce(mean_ref_kl, group=constants.data_parallel_group())
    dist.all_reduce(logging_loss, group=constants.data_parallel_group())
    dist.all_reduce(clip_ratio, group=constants.data_parallel_group())
    dist.all_reduce(normalized_values, group=constants.data_parallel_group())
    dist.all_reduce(denormalized_values, group=constants.data_parallel_group())

    # Update KL coefficient to be consistent with actor.
    kl_adapter.update(mean_ref_kl, n_steps=cu_seqlens.shape[0] - 1)

    return loss, dict(
        value_loss=logging_loss,
        value_clip_ratio=clip_ratio,
        normalized_values=normalized_values,
        denormalized_values=denormalized_values,
    )


@dataclasses.dataclass
class PPOCriticInterface(model_api.ModelInterface):
    n_minibatches: int = 4
    enable_save: bool = True
    kl_ctl: float = 0.1
    discount: float = 1.0
    gae_lambda: float = 0.95
    eps_clip: float = 0.2
    value_eps_clip: float = 0.2
    max_reward_clip: float = 5.0
    adaptive_kl_ctl: bool = False
    adaptive_kl_target: Optional[float] = 6
    adaptive_kl_horizon: Optional[float] = 10000
    value_norm: bool = False
    value_norm_type: str = dataclasses.field(metadata={"choices": ["exp", "ma"]}, default="exp")
    value_norm_beta: float = 0.99995
    value_norm_eps: float = 1e-5

    def __post_init__(self):
        super().__post_init__()
        if self.adaptive_kl_ctl:
            assert self.adaptive_kl_target is not None
            assert self.adaptive_kl_horizon is not None
            self.kl_adapter = ppo_functional.AdaptiveKLController(self.kl_ctl, self.adaptive_kl_target,
                                                                  self.adaptive_kl_horizon)
        else:
            self.kl_adapter = ppo_functional.FixedKLController(self.kl_ctl)
        if self.value_norm:
            from reallm.impl.model.modules import ExponentialRunningMeanStd, MovingAverageRunningMeanStd

            if self.value_norm_type == "exp":
                self.rms = ExponentialRunningMeanStd(beta=self.value_norm_beta, epsilon=self.value_norm_eps)
            elif self.value_norm_type == "ma":
                self.rms = MovingAverageRunningMeanStd()
            else:
                raise ValueError(f"Unknown value_norm_type {self.value_norm_type}")
        self.kl_ctl = None
        self._last_inf_sd = None

    def save(self, model: model_api.Model, save_dir: str):
        if not self.enable_save:
            return
        module = model.module
        if not isinstance(module, ReaLModel):
            module = module.module
        module.save_to_hf(
            tokenizer=model.tokenizer,
            save_dir=save_dir,
        )

    @torch.no_grad()
    def inference(self, model: model_api.Model, data: NamedArray) -> NamedArray:
        module = model.module
        module.eval()
        data = recursive_apply(data, lambda x: x.to(model.device))

        cu_seqlens = data["cu_seqlens"].int()
        input_lens = cu_seqlens[1:] - cu_seqlens[:-1]
        max_seqlen = int(max(input_lens))

        sd = {k: v.detach().clone() for k, v in module.state_dict().items()}
        if self._last_inf_sd is not None:
            param_changed = False
            changed_keys = []
            for k, v1, v2 in zip(sd.keys(), sd.values(), self._last_inf_sd.values()):
                if not torch.allclose(v1, v2):
                    param_changed = True
                    changed_keys.append(k)
            print(">>>>>>>> critic inf param changed?", param_changed)
        self._last_inf_sd = sd   

        if isinstance(module, (PipelinableModelRunner, PipelinableModelRunnerWithZeRO)):
            scores = module.forward(
                seqlens_cpu=data.metadata["seqlens"],
                packed_input_ids=data["packed_seq"],
                cu_seqlens=cu_seqlens,
            )
            if scores is None:
                return None
        else:
            if hasattr(module, "module"):
                module = module.module
            scores: torch.FloatTensor = module(
                packed_input_ids=data["packed_seq"],
                cu_seqlens=cu_seqlens,
                max_seqlen=max_seqlen,
            ).logits
        scores = scores.squeeze(-1)
        res = from_dict(dict(scores=scores))
        res.register_metadata(seqlens=data.metadata["seqlens"])
        return res

    def train_step(self, model: model_api.Model, data_: NamedArray) -> Dict:
        module = model.module
        tokenizer = model.tokenizer
        # We call module.eval() because dropout causes the computation of incorrect of log probs.
        module.eval()
        data_ = recursive_apply(data_, lambda x: x.to(model.device))

        old_logp: torch.FloatTensor = data_["packed_logprobs"].float()
        ref_logp: torch.FloatTensor = data_["packed_ref_logprobs"].float()
        prompt_mask = data_["prompt_mask"]
        cu_seqlens = data_["cu_seqlens"].int()
        reward_score = data_["rewards"].float()
        values = data_["values"].float()
        seq_no_eos_mask = data_["seq_no_eos_mask"]

        if self.value_norm:
            denormalized_values = self.rms.denormalize(values)
        else:
            denormalized_values = values

        for i in range(seq_no_eos_mask.shape[0]):
            if not seq_no_eos_mask[i]:
                # Set value at the EOS token to be zero.
                denormalized_values[cu_seqlens[i + 1] - 1] = 0.0
                values[cu_seqlens[i + 1] - 1] = 0.0

        # Shift the loss mask by one token for each packed sequences.
        input_lens = cu_seqlens[1:] - cu_seqlens[:-1]
        short1cu_seqlens = cu_seqlens.clone()
        short1cu_seqlens[1:] -= torch.ones_like(cu_seqlens[1:]).cumsum(0)
        loss_mask = prompt_mask.logical_not()
        shift_one_indices = torch.cat([
            torch.arange(
                cu_seqlens[i] + 1,
                cu_seqlens[i + 1],
                dtype=torch.long,
                device=cu_seqlens.device,
            ) for i in range(cu_seqlens.shape[0] - 1)
        ])
        loss_mask = loss_mask[shift_one_indices]

        # Apply the mask to log probabilities.
        ref_logp *= loss_mask
        old_logp *= loss_mask

        # Compute rewards and GAEs.
        kl_rewards, rewards = ppo_functional.get_packed_rewards(
            kl_ctl=self.kl_adapter.value,
            clip_reward_value=self.max_reward_clip,
            log_probs=old_logp,
            ref_log_probs=ref_logp,
            reward_score=reward_score,
            short1cu_seqlens=short1cu_seqlens,
            seq_no_eos_mask=seq_no_eos_mask,
        )
        _, returns = ppo_functional.get_packed_advantages_and_returns(
            gamma=self.discount,
            lam=self.gae_lambda,
            values=denormalized_values,
            rewards=rewards,
            short1cu_seqlens=short1cu_seqlens,
            seq_no_eos_mask=seq_no_eos_mask,
        )

        # Optionally perform normalization.
        if self.value_norm:
            self.rms.update(returns, mask=loss_mask)
            normalized_returns = self.rms.normalize(returns)
        else:
            normalized_returns = returns

        # Prepare data to be splitted into mini-batches.
        batch_seqlens = data_.metadata["seqlens"]
        data_ = from_dict(
            dict(
                returns=normalized_returns,
                values=values,
                ppo_loss_mask=loss_mask,
                kl_rewards=kl_rewards,
                packed_seq=data_["packed_seq"],
                cu_seqlens=data_["cu_seqlens"],
            ))
        data_.register_metadata(seqlens=batch_seqlens)
        datas = data_api.split_sequences(
            data_,
            self.n_minibatches,
            min_size=constants.pipe_parallel_world_size() * 2,
        )

        # Logging.
        returns = torch.where(loss_mask, returns, 0.0).sum()
        n_tokens = loss_mask.count_nonzero()
        dist.all_reduce(returns, group=constants.data_parallel_group())
        dist.all_reduce(n_tokens, group=constants.data_parallel_group())
        global_stats = dict(returns=float(returns), n_tokens=int(n_tokens))

        sd = {k: v.detach().clone() for k, v in module.state_dict().items()}
        # NOTE: We cannot randomly shuffle data here because data must the same shape across different pipeline stages.
        train_stats = collections.defaultdict(lambda: 0)
        offset = 0
        for data in datas:
            input_lens = data["cu_seqlens"][1:] - data["cu_seqlens"][:-1]
            if isinstance(module, (PipelinableModelRunner, PipelinableModelRunnerWithZeRO)):
                seqlens_cpu = batch_seqlens[offset:offset + input_lens.shape[0]]
                offset += input_lens.shape[0]
                module.set_version_steps(model.version.global_step)
                module.set_tokenizer(tokenizer)

                loss_kwargs = dict(
                    input_lens=input_lens,
                    values=data["values"],
                    ppo_loss_mask=data["ppo_loss_mask"],
                    returns=data["returns"],
                    kl_rewards=data["kl_rewards"],
                    value_eps_clip=self.value_eps_clip,
                    kl_adapter=self.kl_adapter,
                    rms=self.rms if self.value_norm else None,
                )

                loss, stats = module.train_batch(
                    seqlens_cpu=seqlens_cpu,
                    packed_input_ids=data["packed_seq"],
                    cu_seqlens=data["cu_seqlens"],
                    loss_fn=_ppo_critic_loss_from_model_outputs,
                    **loss_kwargs,
                )
            else:
                max_seqlen = int(max(input_lens))
                new_values: torch.FloatTensor = module(
                    packed_input_ids=data["packed_seq"],
                    cu_seqlens=data["cu_seqlens"],
                    max_seqlen=max_seqlen,
                ).logits.float()

                loss, stats = _ppo_critic_loss_from_model_outputs(
                    new_values=new_values,
                    packed_input_ids=data["packed_seq"],
                    cu_seqlens=data["cu_seqlens"],
                    values=data["values"],
                    ppo_loss_mask=data["ppo_loss_mask"],
                    returns=data["returns"],
                    kl_rewards=data["kl_rewards"],
                    value_eps_clip=self.value_eps_clip,
                    kl_adapter=self.kl_adapter,
                    rms=self.rms if self.value_norm else None,
                )

                with cuda_tmarked("bwd", CUDATimeMarkType.backward):
                    module.backward(loss)
                with cuda_tmarked("optim_step", CUDATimeMarkType.optim_step):
                    module.step(lr_kwargs={"epoch": model.version.global_step})

            if stats:
                for k, v in stats.items():
                    train_stats[k] += v

        sd2 = {k: v.detach().clone() for k, v in module.state_dict().items()}
        param_changed = False
        changed_keys = []
        for k, v1, v2 in zip(sd.keys(), sd.values(), sd2.values()):
            if not torch.allclose(v1, v2):
                param_changed = True
                changed_keys.append(k)
        print(">>>>>>>> critic train param changed?", param_changed)
        cur_epoch = model.version.epoch
        model.inc_version()

        if train_stats:
            train_stats = dict(
                value_loss=float(train_stats["value_loss"] / n_tokens),
                value_clip_ratio=float(train_stats["value_clip_ratio"] / n_tokens),
                normalized_values=float(train_stats["normalized_values"] / n_tokens),
                denormalized_values=float(train_stats["denormalized_values"] / n_tokens),
                returns=global_stats["returns"] / int(n_tokens),
                n_tokens=int(n_tokens),
            )

        return dict(train_stats)


model_api.register_interface("ppo_actor", PPOActorInterface)
model_api.register_interface("ppo_critic", PPOCriticInterface)
