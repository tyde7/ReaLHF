from typing import Callable, Dict, List, Literal, Optional, Union
import collections
import dataclasses
import functools
import logging
import math
import os
import re

import deepspeed
import torch
import torch.nn as nn
import torch.utils.data
import tqdm
import transformers

from base.namedarray import from_dict, NamedArray, recursive_aggregate, recursive_apply
from impl.model.utils import get_eos_indices, masked_normalization, save_hf_model
import api.model
import api.utils

logger = logging.getLogger("WPS Actor Critic")


def remove_code_comments(code: str) -> str:
    prompt_line, *code_lines = code.split('\n')
    lines_to_pop = []
    for i in range(len(code_lines)):
        if "//" not in code_lines[i]:
            if i == 0:
                code_lines[i] = code_lines[i].lstrip()
            continue
        code_lines[i] = code_lines[i][:code_lines[i].index("//")].rstrip()
        if not re.match(r".*[0-9a-zA-Z@].*", code_lines[i]):
            lines_to_pop.append(i)
    for j in reversed(lines_to_pop):
        code_lines.pop(j)
    code = '\n'.join([prompt_line] + code_lines)
    assert "//" not in code
    return code


@dataclasses.dataclass
class WPSRewardUnpairedInterface(api.model.ModelInterface):
    remove_code_comments: bool = False
    pos_weight: float = 1.0

    @torch.inference_mode()
    def inference(self, model: api.model.Model, data: NamedArray) -> NamedArray:
        module = model.module
        module.eval()
        data = recursive_apply(data, lambda x: x.to(model.device))
        scores: torch.FloatTensor = module(input_ids=data['input_ids'],
                                           attention_mask=data['attention_mask']).float()
        prompt_len = data['prompts'].size()[-1]
        eos_indices, _ = get_eos_indices(data['input_ids'][:, prompt_len:], model.tokenizer) + prompt_len
        chosen_end_scores = scores.gather(-1,
                                          eos_indices.unsqueeze(-1)).squeeze(-1) / 10  # FIXME: for debug only

        # FIXME: for debug only
        ###################### logging ######################
        # seq_strs = model.tokenizer.batch_decode(data['input_ids'],
        #                                         clean_up_tokenization_spaces=False,
        #                                         skip_special_tokens=True)
        # for seq_str, score in zip(seq_strs, chosen_end_scores):
        #     logger.info(f"reward is {score.item()}, sequence is: {seq_str}")
        #####################################################

        return from_dict(dict(scores=chosen_end_scores.cpu()))

    def train_step(self, model: api.model.Model, batch: NamedArray) -> NamedArray:
        device = model.device
        rm_model = model.module
        rm_model.train()

        if self.remove_code_comments:
            max_token_len = batch['input_ids'].shape[1]
            seq_strs = model.tokenizer.batch_decode(batch['input_ids'],
                                                    clean_up_tokenization_spaces=False,
                                                    skip_special_tokens=True)
            for j, seq_str in enumerate(seq_strs):
                seq_str = remove_code_comments(seq_str)
                seq_strs[j] = seq_str + model.tokenizer.eos_token

            tokens = model.tokenizer(seq_strs,
                                     max_length=max_token_len,
                                     padding="max_length",
                                     truncation=True,
                                     return_tensors="pt")
            batch['input_ids'] = tokens['input_ids']
            batch['attention_mask'] = tokens['attention_mask']

        batch = recursive_apply(batch, lambda x: x.to(device))
        labels = batch['correctness_labels']
        eos_indices, _ = get_eos_indices(batch['input_ids'], model.tokenizer)

        scores = rm_model(input_ids=batch['input_ids'],
                          attention_mask=batch['attention_mask'],
                          use_cache=False)
        scores = torch.gather(scores, -1, eos_indices.unsqueeze(-1)).squeeze(-1)

        bs = batch['input_ids'].shape[0]
        pos_weight = self.pos_weight
        loss = torch.nn.functional.binary_cross_entropy_with_logits(
            scores,
            labels.half(),
            pos_weight=torch.full((bs,), fill_value=pos_weight, dtype=torch.half, device=scores.device),
        )
        rm_model.backward(loss)
        rm_model.step()

        cur_epoch = model.version.epoch
        model.inc_version()
        if model.version.epoch > cur_epoch:
            rm_model.tput_timer.update_epoch_count()

        return dict(loss=loss.detach().item())

    def save(self, model: api.model.Model, output_dir):
        save_hf_model(model, output_dir)

    @torch.inference_mode()
    def evaluate(self, model_: api.model.Model, eval_dataloader: torch.utils.data.DataLoader) -> Dict:
        device = model_.device
        model = model_.module

        model.eval()
        correct_predictions = 0
        total_predictions = 0

        for step, batch in enumerate(tqdm.tqdm(eval_dataloader)):
            batch = recursive_apply(from_dict(batch), lambda x: x.to(device))
            labels = batch['correctness_labels']
            eos_indices, _ = get_eos_indices(batch['input_ids'], model_.tokenizer)
            scores = model(
                input_ids=batch['input_ids'],
                attention_mask=batch['attention_mask'],
            )
            scores = torch.gather(scores, -1, eos_indices.unsqueeze(-1)).squeeze(-1)
            bs = scores.shape[0]
            correct_predictions += ((scores > 0.0) == labels).sum()
            total_predictions += bs

        return dict(acc=float(correct_predictions / total_predictions))


api.model.register_interface("wps_reward_unpaired", WPSRewardUnpairedInterface)


def gather_shifted_log_probs(logits: torch.FloatTensor, labels: torch.LongTensor) -> torch.FloatTensor:
    logits = logits[:, :-1]
    labels = labels[:, 1:]
    log_probs = torch.nn.functional.log_softmax(logits, dim=-1)
    log_probs_labels = log_probs.gather(dim=-1, index=labels.unsqueeze(-1))
    return log_probs_labels.squeeze(-1)


def generate_logits_ignoring_mask(logits: torch.FloatTensor,
                                  top_p: Optional[float] = 1.0,
                                  top_k: Optional[int] = -1) -> torch.BoolTensor:
    if top_p is None:
        top_p = 1.0
    if top_k is None:
        top_k = -1
    assert 0 < top_p <= 1.0
    if top_k < 0 or top_k > logits.size(-1):
        top_k = logits.size(-1)
    if top_p == 1.0 and top_k == logits.size(-1):
        return torch.zeros_like(logits, dtype=torch.bool)

    sorted_logits, sorted_indices = torch.sort(logits, descending=False, dim=-1)
    sorted_logits: torch.FloatTensor
    sorted_indices: torch.LongTensor
    cumulative_probs = sorted_logits.softmax(dim=-1).cumsum(dim=-1)
    # Remove tokens with cumulative top_p above the threshold (token with 0 are kept)
    sorted_indices_to_remove = cumulative_probs <= (1 - top_p)
    # scatter sorted tensors to original indexing
    top_p_indices_to_remove = sorted_indices_to_remove.scatter(-1, sorted_indices, sorted_indices_to_remove)

    # Remove all tokens with a probability less than the last token of the top-k
    top_k_indices_to_remove = logits < torch.topk(logits, top_k)[0][..., -1, None]

    return top_p_indices_to_remove.logical_or(top_k_indices_to_remove).bool()


def actor_loss_fn(logprobs: torch.FloatTensor, old_logprobs: torch.FloatTensor, advantages: torch.FloatTensor,
                  loss_mask: torch.FloatTensor, eps_clip: float):
    ratio = torch.exp((logprobs - old_logprobs) * loss_mask)
    clipped_ratio = torch.clamp(ratio, 1.0 - eps_clip, 1.0 + eps_clip)
    pg_loss1 = -advantages * ratio
    pg_loss2 = -advantages * clipped_ratio
    pg_loss = (torch.max(pg_loss1, pg_loss2) * loss_mask).sum() / loss_mask.sum()
    proportion_clipped = (pg_loss1 < pg_loss2)
    proportion_clipped = (proportion_clipped.float() * loss_mask).sum() / loss_mask.sum()
    return pg_loss, proportion_clipped, (ratio.detach() * loss_mask).sum() / loss_mask.sum()


def critic_loss_fn(value: torch.FloatTensor, old_value: torch.FloatTensor, target_value: torch.FloatTensor,
                   loss_mask: torch.FloatTensor, value_eps_clip: float) -> torch.FloatTensor:
    # TODO: support both huber and mse
    value_loss_original = torch.nn.functional.huber_loss(value, target_value, reduction='none', delta=10.0)
    # value_loss_original = (value - target_value).pow(2)
    value_clipped = old_value + (value - old_value).clamp(-value_eps_clip, value_eps_clip)
    # value_loss_clipped = (value_clipped - target_value).pow(2)
    value_loss_clipped = torch.nn.functional.huber_loss(value_clipped,
                                                        target_value,
                                                        reduction='none',
                                                        delta=10.0)
    # value_loss = torch.max(value_loss_original, value_loss_clipped)
    value_loss = value_loss_original
    proportion_clipped = (value_loss_clipped > value_loss_original)
    proportion_clipped = (proportion_clipped.float() * loss_mask).sum() / loss_mask.sum()
    return 0.5 * (value_loss * loss_mask).sum() / loss_mask.sum(), proportion_clipped


@torch.inference_mode()
def compute_rewards(kl_ctl: float, clip_reward_value: float, log_probs: torch.FloatTensor,
                    ref_log_probs: torch.FloatTensor, reward_score: torch.FloatTensor,
                    eos_indices: torch.LongTensor, seq_no_eos_mask: torch.FloatTensor):
    kl_rewards = -kl_ctl * (log_probs - ref_log_probs)
    for i in range(kl_rewards.shape[0]):
        kl_rewards[i, eos_indices[i]:] = 0.0
    score_rewards = torch.zeros_like(kl_rewards)
    reward_clip = torch.clamp(reward_score, -clip_reward_value, clip_reward_value)
    # This is assigned to the token before EOS, which rewards the output of the EOS token.
    score_rewards.scatter_(-1, (eos_indices - 1).unsqueeze(-1), reward_clip.unsqueeze(-1))
    score_rewards = score_rewards * (1 - seq_no_eos_mask.unsqueeze(1))  # only compute final rewards with EOS
    return kl_rewards, kl_rewards + score_rewards


@torch.inference_mode()
def get_advantages_and_returns(gamma: float, lam: float, values: torch.FloatTensor,
                               rewards: torch.FloatTensor):
    # Adopted from https://github.com/CarperAI/trlx/blob/main/trlx/models/modeling_ppo.py#L134
    assert values.shape[1] == rewards.shape[1] + 1
    lastgaelam = 0
    advantages_reversed = []
    length = rewards.size()[-1]
    for t in reversed(range(length)):
        nextvalues = values[:, t + 1]
        delta = rewards[:, t] + gamma * nextvalues - values[:, t]
        lastgaelam = delta + gamma * lam * lastgaelam
        advantages_reversed.append(lastgaelam)
    advantages = torch.stack(advantages_reversed[::-1], dim=1)
    returns = advantages + values
    return advantages.detach(), returns


@dataclasses.dataclass
class WPSActorInterface(api.model.ModelInterface):
    mini_batch_size: int = 8
    ppo_epochs: int = 1
    kl_ctl: float = 0.1
    discount: float = 1.0
    gae_lambda: float = 0.95
    eps_clip: float = 0.2
    value_eps_clip: float = 0.2
    max_reward_clip: float = 5.0

    @torch.inference_mode()
    def generate(self, model: api.model.Model, data: NamedArray) -> NamedArray:
        module = model.module
        tokenizer = model.tokenizer
        module.eval()
        module = module.module if isinstance(module, deepspeed.DeepSpeedEngine) else module

        assert module.generation_config.pad_token_id == model.tokenizer.pad_token_id
        assert module.generation_config.eos_token_id == model.tokenizer.eos_token_id
        if module.generation_config.max_new_tokens is not None:
            max_token_len = module.generation_config.max_new_tokens + data.prompts.shape[1]
        else:
            max_token_len = module.generation_config.max_length

        data = recursive_apply(data, lambda x: x.to(model.device))
        seq = module.generate(data.prompts,
                              attention_mask=data.prompt_att_mask,
                              generation_config=module.generation_config)

        pad_token_id = model.tokenizer.pad_token_id
        eos_token_id = model.tokenizer.eos_token_id
        pad_length = max_token_len - seq.shape[1]
        if pad_length > 0:
            seq = torch.nn.functional.pad(seq, pad=(0, pad_length), mode='constant', value=pad_token_id)
        attention_mask = torch.logical_and(seq.not_equal(pad_token_id), (seq.not_equal(eos_token_id))).long()

        module.train()
        logits: torch.FloatTensor = module(input_ids=seq, attention_mask=attention_mask).logits.float()
        logits_ignoring_mask = generate_logits_ignoring_mask(logits, module.generation_config.top_p,
                                                             module.generation_config.top_k)
        logits.masked_fill_(logits_ignoring_mask.bool(), torch.finfo(logits.dtype).min)
        logp = gather_shifted_log_probs(logits, seq)

        res = from_dict(
            dict(
                seq=seq,
                attention_mask=attention_mask,
                logp=logp,
                logits_ignoring_mask=logits_ignoring_mask,
            ),)
        return recursive_apply(res, lambda x: x.cpu())

    @torch.inference_mode()
    def inference(self, model: api.model.Model, data: NamedArray) -> NamedArray:
        module = model.module
        module.eval()
        data = recursive_apply(data, lambda x: x.to(model.device))
        logits: torch.FloatTensor = module(input_ids=data['input_ids'],
                                           attention_mask=data['attention_mask']).logits.float()
        logits.masked_fill_(data['logits_ignoring_mask'].bool(), torch.finfo(logits.dtype).min)
        logp = gather_shifted_log_probs(logits, data['input_ids'])
        return from_dict(dict(logp=logp.cpu()))

    def _ppo_actor_step(self, ppo_epoch: int, module: api.model.NeuralNetwork,
                        tokenizer: transformers.PreTrainedTokenizerFast, sample: NamedArray) -> Dict:
        module.train()
        logits_ignoring_mask = sample['logits_ignoring_mask']
        new_logits: torch.FloatTensor = module(input_ids=sample['input_ids'],
                                               attention_mask=sample['attention_mask'],
                                               use_cache=False).logits.float()
        new_logits.masked_fill_(logits_ignoring_mask.bool(), torch.finfo(new_logits.dtype).min)
        new_logp = gather_shifted_log_probs(new_logits, sample['input_ids'])

        old_logp: torch.Tensor = sample['logp']
        ref_logp: torch.Tensor = sample['ref_logp']

        prompt_len = sample['prompts'].size()[-1]
        shifted_start = prompt_len - 1
        loss_mask = sample['attention_mask'][:, 1:].clone()
        loss_mask[:, :shifted_start] = 0

        eos_indices, seq_no_eos_mask = get_eos_indices(sample['input_ids'][:, prompt_len:], tokenizer)
        eos_indices = eos_indices + prompt_len

        kl_rewards, rewards = compute_rewards(self.kl_ctl, self.max_reward_clip, old_logp, ref_logp,
                                              sample['rewards'], eos_indices, seq_no_eos_mask)
        advantages, returns = get_advantages_and_returns(self.discount, self.gae_lambda,
                                                         sample['values'][:, shifted_start:],
                                                         rewards[:, shifted_start:])
        # adv_norm = masked_normalization(advantages)
        adv_norm = advantages
        adv_norm = torch.cat([torch.zeros_like(sample['values'][:, :shifted_start]), adv_norm], dim=1)

        loss, clip_ratio, importance_weight = actor_loss_fn(new_logp, old_logp, adv_norm, loss_mask,
                                                            self.eps_clip)

        module.backward(loss)
        module.step()

        prompts: torch.LongTensor = sample['prompts']
        ans: torch.LongTensor = sample['input_ids'][:, prompt_len:]
        prompt_non_pad_ratio = (prompts != tokenizer.pad_token_id).float().mean()
        prompt_truncate_ratio = (prompts[:, 0] != tokenizer.pad_token_id).float().mean()
        generated_non_pad_ratio = (ans != tokenizer.pad_token_id).float().mean()
        generated_truncate_ratio = (ans[:, -1] != tokenizer.pad_token_id).float().mean()

        ignoring_logits_ratio = logits_ignoring_mask.float().mean()

        return dict(
            task_reward=sample['rewards'].mean().detach(),
            advantage=advantages.mean().detach(),
            reward=rewards.mean().detach(),
            kl_reward=kl_rewards.mean().detach(),
            actor_loss=loss.detach(),
            actor_clip_ratio=clip_ratio.detach(),
            importance_weight=importance_weight.detach(),
            prompt_non_pad_ratio=prompt_non_pad_ratio,
            prompt_truncate_ratio=prompt_truncate_ratio,
            generated_non_pad_ratio=generated_non_pad_ratio,
            generated_truncate_ratio=generated_truncate_ratio,
            ignoring_logits_ratio=ignoring_logits_ratio,
        )

    def train_step(self, model_: api.model.Model, sample: NamedArray) -> Dict:
        # TODO: add imitation learning auxilary loss
        # TODO: add EMA
        model = model_.module
        tokenizer = model_.tokenizer
        model.train()
        assert sample['input_ids'].shape[0] % self.mini_batch_size == 0
        n_minibatch = sample['input_ids'].shape[0] // self.mini_batch_size

        sample = recursive_apply(sample, lambda x: x.to(model_.device))

        train_stats = collections.defaultdict(lambda: 0)
        for ppo_i in range(self.ppo_epochs):
            shuffle_indices = torch.randperm(sample['input_ids'].shape[0])
            for mini_bs_i in range(0, sample['input_ids'].shape[0], self.mini_batch_size):
                indices = shuffle_indices[mini_bs_i:mini_bs_i + self.mini_batch_size]
                stats = self._ppo_actor_step(ppo_i, model, tokenizer, sample[indices])
                for k, v in stats.items():
                    train_stats[k] += v

        cur_epoch = model_.version.epoch
        model_.inc_version()
        if model_.version.epoch > cur_epoch:
            model.tput_timer.update_epoch_count()

        train_stats = dict(train_stats)
        for k, v in train_stats.items():
            v = v.detach() / self.ppo_epochs / n_minibatch
            train_stats[k] = api.utils.get_all_reduce_mean(v).item()

        return train_stats

    def save(self, model: api.model.Model, output_dir):
        save_hf_model(model, output_dir)


api.model.register_interface("wps_actor", WPSActorInterface)


@dataclasses.dataclass
class WPSCriticInterface(api.model.ModelInterface):
    mini_batch_size: int = 8
    ppo_epochs: int = 1
    kl_ctl: float = 0.1
    discount: float = 1.0
    gae_lambda: float = 0.95
    eps_clip: float = 0.2
    value_eps_clip: float = 0.2
    max_reward_clip: float = 5.0

    @torch.inference_mode()
    def inference(self, model: api.model.Model, data: NamedArray) -> NamedArray:
        module = model.module
        module.train()
        data = recursive_apply(data, lambda x: x.to(model.device))
        scores = module(input_ids=data['input_ids'],
                        attention_mask=data['attention_mask']).float() / 10  # FIXME: for debug only

        # FIXME: for debug only
        debug_id = torch.zeros(data['input_ids'].shape[0], dtype=torch.long, device=model.device)
        if torch.distributed.get_rank() == 0:
            debug_id[0] = 1
            logger.info(f"inference seq: {data['input_ids'][0]}, attn mask: {data['attention_mask'][0]}")

        prompt_len = data['prompts'].shape[1]
        eos_indices, seq_no_eos_mask = get_eos_indices(data['input_ids'][:, prompt_len:],
                                                       model.tokenizer) + prompt_len
        for i in range(scores.shape[0]):
            if not seq_no_eos_mask[i]:
                scores[i, eos_indices[i]:] = 0
        return from_dict(dict(scores=scores, debug_id=debug_id))

    def _ppo_critic_step(self, ppo_epoch: int, module: api.model.NeuralNetwork,
                         tokenizer: transformers.PreTrainedTokenizerFast, sample: NamedArray) -> Dict:
        module.train()
        new_values = module(input_ids=sample['input_ids'],
                            attention_mask=sample['attention_mask'],
                            use_cache=False).float() / 10  # FIXME: for debug only

        old_logp: torch.Tensor = sample['logp']
        ref_logp: torch.Tensor = sample['ref_logp']

        prompt_len = sample['prompts'].size()[-1]
        shifted_start = prompt_len - 1
        loss_mask = sample['attention_mask'][:, 1:].clone()
        loss_mask[:, :shifted_start] = 0

        eos_indices, seq_no_eos_mask = get_eos_indices(sample['input_ids'][:, prompt_len:], tokenizer)
        eos_indices = eos_indices + prompt_len

        _, rewards = compute_rewards(self.kl_ctl, self.max_reward_clip, old_logp, ref_logp, sample['rewards'],
                                     eos_indices, seq_no_eos_mask)
        _, returns = get_advantages_and_returns(self.discount, self.gae_lambda,
                                                sample['values'][:, shifted_start:], rewards[:,
                                                                                             shifted_start:])
        returns = torch.cat([torch.zeros_like(sample['values'][:, :shifted_start]), returns], dim=1)

        loss, clip_ratio = critic_loss_fn(new_values[:, :-1], sample['values'][:, :-1], returns, loss_mask,
                                          self.value_eps_clip)

        # FIXME: for debug only
        if torch.any(sample['debug_id'] == 1):
            idx = sample['debug_id'].argmax()
            logger.info(f"output values: {new_values[idx, :-1] * loss_mask[idx]}, "
                        f"old values: {sample['values'][idx, :-1] * loss_mask[idx]}, "
                        f"returns: {returns[idx] * loss_mask[idx]}, loss_mask: {loss_mask[idx]}")
            logger.info(f"seq: {sample['input_ids'][idx]}, attention_mask: {sample['attention_mask'][idx]}")

        # FIXME: for debug only
        # module.backward(loss)
        # module.step()

        return dict(
            value_loss=loss.detach(),
            value_clip_ratio=clip_ratio.detach(),
            values=new_values.mean().detach(),
            returns=returns.mean().detach(),
        )

    def train_step(self, model_: api.model.Model, sample: NamedArray) -> Dict:
        model = model_.module
        tokenizer = model_.tokenizer
        model.train()
        assert sample['input_ids'].shape[0] % self.mini_batch_size == 0
        n_minibatch = sample['input_ids'].shape[0] // self.mini_batch_size

        sample = recursive_apply(sample, lambda x: x.to(model_.device))

        train_stats = collections.defaultdict(lambda: 0)
        for ppo_i in range(self.ppo_epochs):
            shuffle_indices = torch.randperm(sample['input_ids'].shape[0])
            for mini_bs_i in range(0, sample['input_ids'].shape[0], self.mini_batch_size):
                indices = shuffle_indices[mini_bs_i:mini_bs_i + self.mini_batch_size]
                stats = self._ppo_critic_step(ppo_i, model, tokenizer, sample[indices])
                for k, v in stats.items():
                    train_stats[k] += v

        cur_epoch = model_.version.epoch
        model_.inc_version()
        if model_.version.epoch > cur_epoch:
            model.tput_timer.update_epoch_count()

        train_stats = dict(train_stats)
        for k, v in train_stats.items():
            v = v.detach() / self.ppo_epochs / n_minibatch
            train_stats[k] = api.utils.get_all_reduce_mean(v).item()

        return train_stats

    def save(self, model: api.model.Model, output_dir):
        save_hf_model(model, output_dir)


api.model.register_interface('wps_critic', WPSCriticInterface)


@dataclasses.dataclass
class WPSContrastiveRewardInterface(api.model.ModelInterface):

    @torch.inference_mode()
    def inference(self, model: api.model.Model, data: NamedArray) -> NamedArray:
        module = model.module
        module.eval()
        data = recursive_apply(data, lambda x: x.to(model.device))

        bs, prompt_len = data['prompts'].size()
        eos_indices, _ = get_eos_indices(data['input_ids'][:, prompt_len:], model.tokenizer)

        chosen_end_scores = module(
            prompts=data['input_ids'][:, :prompt_len],
            prompt_attention_mask=data['attention_mask'][:, :prompt_len],
            responses=data['input_ids'][:, prompt_len:],
            response_attention_mask=data['attention_mask'][:, prompt_len:],
            eos_indices=eos_indices,
        )

        ###################### logging ######################
        seq_strs = model.tokenizer.batch_decode(data['input_ids'],
                                                clean_up_tokenization_spaces=False,
                                                skip_special_tokens=True)
        for seq_str, score in zip(seq_strs, chosen_end_scores):
            logger.info(f"reward is {score.item()}, sequence is: {seq_str}")
        #####################################################

        return from_dict(dict(scores=chosen_end_scores.cpu()))

    def train_step(self, model: api.model.Model, batch: NamedArray) -> NamedArray:
        device = model.device
        rm_model = model.module
        rm_model.train()

        batch = recursive_apply(batch, lambda x: x.to(device))
        labels = batch['labels']

        bs, c_dim = batch['responses'].shape[:2]
        eos_indices, _ = get_eos_indices(batch['responses'].flatten(end_dim=1), model.tokenizer)
        eos_indices = eos_indices.view(bs, c_dim, *eos_indices.shape[1:])

        scores = rm_model(
            prompts=batch['prompts'],
            prompt_attention_mask=batch['prompt_attention_mask'],
            responses=batch['responses'],
            response_attention_mask=batch['response_attention_mask'],
            eos_indices=eos_indices,
        )
        scores = torch.cat([torch.zeros((bs, 1), dtype=scores.dtype, device=scores.device), scores], dim=1)
        loss = torch.nn.functional.cross_entropy(scores, labels, reduction='mean')

        rm_model.backward(loss)
        rm_model.step()

        cur_epoch = model.version.epoch
        model.inc_version()
        if model.version.epoch > cur_epoch:
            rm_model.tput_timer.update_epoch_count()

        return dict(loss=loss.detach().item(), acc=(scores.argmax(-1) == labels).mean().detach().item())

    def save(self, model: api.model.Model, output_dir):
        module = model.module
        tokenizer = model.tokenizer
        logger.info(f'saving the model for epoch {model.version.epoch} step {model.version.epoch_step}...')
        model_to_save = module.module if hasattr(module, 'module') else module
        output_dir = os.path.join(output_dir, f"epoch{model.version.epoch}step{model.version.epoch_step}")
        os.makedirs(output_dir, exist_ok=True)
        output_model_file = os.path.join(output_dir, "pytorch_model.bin")
        output_config_file = os.path.join(output_dir, "config.json")
        save_dict = model_to_save.state_dict()
        torch.save(save_dict, output_model_file)
        model_to_save.config.to_json_file(output_config_file)
        tokenizer.save_vocabulary(output_dir)

    @torch.inference_mode()
    def evaluate(self, model_: api.model.Model, eval_dataloader: torch.utils.data.DataLoader) -> Dict:
        device = model_.device
        model = model_.module
        tokenizer = model_.tokenizer

        model.eval()
        correct_predictions = 0
        total_predictions = 0
        loss = 0

        for step, batch in enumerate(tqdm.tqdm(eval_dataloader)):
            batch = recursive_apply(from_dict(batch), lambda x: x.to(device))
            labels = batch['labels']
            bs, c_dim = batch['responses'].shape[:2]
            eos_indices, _ = get_eos_indices(batch['responses'].flatten(end_dim=1), tokenizer)
            eos_indices = eos_indices.view(bs, c_dim, *eos_indices.shape[1:])

            scores: torch.FloatTensor = model(
                prompts=batch['prompts'],
                prompt_attention_mask=batch['prompt_attention_mask'],
                responses=batch['responses'],
                response_attention_mask=batch['response_attention_mask'],
                eos_indices=eos_indices,
            )
            scores = torch.cat([torch.zeros((bs, 1), dtype=scores.dtype, device=scores.device), scores],
                               dim=1)
            loss += torch.nn.functional.cross_entropy(scores, labels, reduction='sum')
            correct_predictions += (scores.argmax(-1) == labels).sum()
            total_predictions += bs

        return dict(acc=float(correct_predictions / total_predictions), loss=float(loss / total_predictions))


api.model.register_interface("wps_contrastive_reward", WPSContrastiveRewardInterface)


@dataclasses.dataclass
class WPSPlackettLuceRewardInterface(api.model.ModelInterface):

    def __post_init__(self):
        self.train_total_correct_predictions = 0
        self.train_total_predictions = 0

    @torch.inference_mode()
    def inference(self, model: api.model.Model, data: NamedArray) -> NamedArray:
        module = model.module
        module.eval()
        data = recursive_apply(data, lambda x: x.to(model.device))
        scores: torch.FloatTensor = module(input_ids=data['input_ids'],
                                           attention_mask=data['attention_mask']).float()
        prompt_len = data['prompts'].size()[-1]
        eos_indices, _ = get_eos_indices(data['input_ids'][:, prompt_len:], model.tokenizer) + prompt_len
        chosen_end_scores = scores.gather(-1, eos_indices.unsqueeze(-1)).squeeze(-1)

        ###################### logging ######################
        seq_strs = model.tokenizer.batch_decode(data['input_ids'],
                                                clean_up_tokenization_spaces=False,
                                                skip_special_tokens=True)
        for seq_str, score in zip(seq_strs, chosen_end_scores):
            logger.info(f"reward is {score.item()}, sequence is: {seq_str}")
        #####################################################

        return from_dict(dict(scores=chosen_end_scores.cpu()))

    def train_step(self, model: api.model.Model, batch: NamedArray) -> NamedArray:
        device = model.device
        rm_model = model.module
        rm_model.train()

        batch = recursive_apply(batch, lambda x: x.to(device))
        labels = batch['labels']

        bs, c_dim = batch['input_ids'].shape[:2]
        eos_indices, _ = get_eos_indices(batch['input_ids'].flatten(end_dim=1), model.tokenizer)
        eos_indices = eos_indices.view(bs, c_dim)

        scores: torch.FloatTensor = rm_model(
            input_ids=batch['input_ids'].flatten(end_dim=1),
            attention_mask=batch['attention_mask'].flatten(end_dim=1),
            use_cache=False,
        ).float()  # [bs * c_dim, seq_len]

        scores = scores.view(bs, c_dim, -1)
        scores = scores.gather(-1, eos_indices.unsqueeze(-1)).squeeze(-1)  # [bs, c_dim]

        scores = torch.cat([torch.zeros((bs, 1), dtype=scores.dtype, device=scores.device), scores], dim=1)
        loss = torch.nn.functional.cross_entropy(scores, labels, reduction='mean')
        logger.info(f"scores: {scores}, loss: {loss}.")

        rm_model.backward(loss)
        rm_model.step()

        correct_predictions = (scores.argmax(-1) == labels).float().sum().detach().item()
        self.train_total_correct_predictions += correct_predictions
        self.train_total_predictions += bs
        acc = self.train_total_correct_predictions / self.train_total_predictions

        cur_epoch = model.version.epoch
        model.inc_version()
        if model.version.epoch > cur_epoch:
            rm_model.tput_timer.update_epoch_count()
            self.train_total_predictions = self.train_total_correct_predictions = 0

        return dict(loss=loss.detach().item(), acc=acc)

    def save(self, model: api.model.Model, output_dir):
        save_hf_model(model, output_dir)

    @torch.inference_mode()
    def evaluate(self, model_: api.model.Model, eval_dataloader: torch.utils.data.DataLoader) -> Dict:
        device = model_.device
        model = model_.module
        tokenizer = model_.tokenizer

        model.eval()
        correct_predictions = 0
        total_predictions = 0
        loss = 0

        for step, batch in enumerate(tqdm.tqdm(eval_dataloader)):
            batch = recursive_apply(from_dict(batch), lambda x: x.to(device))
            labels = batch['labels']
            bs, c_dim = batch['input_ids'].shape[:2]

            eos_indices, _ = get_eos_indices(batch['input_ids'].flatten(end_dim=1), tokenizer)
            eos_indices = eos_indices.view(bs, c_dim)

            scores: torch.FloatTensor = model(
                input_ids=batch['input_ids'].flatten(end_dim=1),
                attention_mask=batch['attention_mask'].flatten(end_dim=1),
                use_cache=False,
            ).float().view(bs, c_dim, -1)
            scores = scores.gather(-1, eos_indices.unsqueeze(-1)).squeeze(-1)
            scores = torch.cat([torch.zeros((bs, 1), dtype=scores.dtype, device=scores.device), scores],
                               dim=1)
            loss += torch.nn.functional.cross_entropy(scores, labels, reduction='sum')
            correct_predictions += (scores.argmax(-1) == labels).sum()
            total_predictions += bs

        return dict(acc=float(correct_predictions / total_predictions), loss=float(loss / total_predictions))


api.model.register_interface("wps_plackett_luce_reward", WPSPlackettLuceRewardInterface)