from typing import Dict, Optional
import dataclasses
import os

import colorama
import deepspeed
import torch
import tqdm

from reallm.base.namedarray import from_dict, NamedArray, recursive_apply
from reallm.impl.model.backend.pipe_engine.ds_pipe_engine import DeepSpeedPipelineEngine
from reallm.impl.model.backend.pipe_inf import InferencePipelineEngine
from reallm.impl.model.nn.real_llm_api import ReaLModel
import reallm.api.core.model_api as model_api
import reallm.base.logging as logging

logger = logging.getLogger("Packed Reward Modeling Interface", "benchmark")


def _paired_rw_loss_from_model_outputs(
    scores: torch.FloatTensor,
    packed_input_ids: torch.LongTensor,
    cu_seqlens: torch.IntTensor,
    group_factor: torch.FloatTensor,
    **kwargs,
):
    scores = scores[cu_seqlens[1:] - 1].view(-1, 2).float()
    loss = -(torch.nn.functional.logsigmoid(scores[:, 0] - scores[:, 1]) * group_factor).sum()
    correct_predictions = (scores[:, 0] > scores[:, 1]).count_nonzero().detach().float()
    return loss, dict(
        loss=loss.cpu(),
        correct_predictions=correct_predictions.cpu(),
        avg_pos_score=scores[:, 0].mean().detach().cpu(),
        avg_neg_score=scores[:, 1].mean().detach().cpu(),
    )


@dataclasses.dataclass
class PairedRewardInterface(model_api.ModelInterface):
    enable_save: bool = True

    output_scaling: float = 1.0
    output_bias: float = 0.0

    # training log
    train_total_predictions: int = 0
    train_total_correct_predictions: int = 0

    @torch.no_grad()
    def inference(self, model: model_api.Model, data: NamedArray) -> NamedArray:
        data = recursive_apply(data, lambda x: x.to(model.device))
        packed_input_ids: torch.Tensor = data["packed_input_ids"]
        seqlens_cpu = data.metadata["seqlens"]
        max_seqlen = max(seqlens_cpu)
        cu_seqlens = torch.nn.functional.pad(
            torch.tensor(seqlens_cpu, dtype=torch.int32, device=model.device).cumsum(0),
            (1, 0),
        )

        module: deepspeed.DeepSpeedEngine = model.module

        module.eval()

        if isinstance(module, (InferencePipelineEngine, DeepSpeedPipelineEngine)):
            r = module.forward(
                seqlens_cpu=data.metadata["seqlens"],
                packed_input_ids=packed_input_ids,
                cu_seqlens=cu_seqlens,
            )
            if r is None:
                return
            scores = r.float()
        else:
            if hasattr(module, "module"):
                module = module.module
            scores: torch.FloatTensor = module(
                packed_input_ids=packed_input_ids,
                cu_seqlens=cu_seqlens,
                max_seqlen=max_seqlen,
            ).logits

        chosen_end_scores = scores.squeeze(-1)[cu_seqlens[1:] - 1].float()  # [bs]
        scores = (scores - self.output_bias) * self.output_scaling

        ###################### logging ######################
        # input_ids = [packed_input_ids[start:end] for start, end in zip(cu_seqlens[:-1], cu_seqlens[1:])]
        # seq_strs = model.tokenizer.batch_decode(input_ids,
        #                                         clean_up_tokenization_spaces=False,
        #                                         skip_special_tokens=True)
        # for seq_str, score in zip(seq_strs, chosen_end_scores):
        #     logger.info(
        #         f"reward is {colorama.Fore.RED}{score.item()}{colorama.Style.RESET_ALL}, sequence is: {colorama.Fore.YELLOW + colorama.Style.DIM}{seq_str}{colorama.Style.RESET_ALL}"
        #     )
        #####################################################

        res = from_dict(dict(scores=chosen_end_scores))
        res.register_metadata(**data.metadata)
        return res

    def train_step(self, model: model_api.Model, data: NamedArray) -> NamedArray:
        data = recursive_apply(data, lambda x: x.to(model.device))

        packed_input_ids: torch.Tensor = data["packed_input_ids"]
        pair_lens = torch.tensor(data.metadata["seqlens"], dtype=torch.int32, device=model.device)
        neg_input_lens = pair_lens - data["pos_input_lens"]
        input_lens: torch.Tensor = torch.stack([data["pos_input_lens"], neg_input_lens], 1).view(-1)
        group_factor: torch.Tensor = data["group_factor"]
        cu_seqlens = torch.cat([input_lens.new_zeros(1), input_lens.cumsum(0)], 0).int()
        max_seqlen = int(max(cu_seqlens[1:] - cu_seqlens[:-1]))

        module = model.module
        module.train()

        if isinstance(module, DeepSpeedPipelineEngine):
            loss_fn_kwargs = dict(
                input_lens=pair_lens,
                group_factor=data["group_factor"],
            )
            loss, stats = module.train_batch(
                seqlens_cpu=data.metadata["seqlens"],
                packed_input_ids=packed_input_ids,
                cu_seqlens=cu_seqlens,
                loss_fn=_paired_rw_loss_from_model_outputs,
                input_lens_for_partition=pair_lens,
                **loss_fn_kwargs,
            )
        else:
            scores: torch.FloatTensor = module(
                packed_input_ids=packed_input_ids,
                cu_seqlens=cu_seqlens,
                max_seqlen=max_seqlen,
            ).logits
            loss, stats = _paired_rw_loss_from_model_outputs(scores, packed_input_ids, cu_seqlens,
                                                             group_factor)

            module.backward(loss)
            module.step()

        if stats is not None:
            self.train_total_correct_predictions += stats["correct_predictions"].item()
            assert input_lens.shape[0] % 2 == 0
            self.train_total_predictions += input_lens.shape[0] // 2
            acc = self.train_total_correct_predictions / self.train_total_predictions
            stats["acc"] = acc

        cur_epoch = model.version.epoch
        model.inc_version()
        if model.version.epoch > cur_epoch:
            module.tput_timer.update_epoch_count()
            self.train_total_predictions = self.train_total_correct_predictions = 0

        if stats is None:
            stats = {}
        return {k: float(v) for k, v in stats.items()}

    def save(self, model: model_api.Model, save_dir: str):
        module = model.module
        if not isinstance(module, ReaLModel):
            module = module.module
        module.save_to_hf(
            tokenizer=model.tokenizer,
            save_dir=save_dir,
        )

    @torch.no_grad()
    def evaluate(self, model_: model_api.Model, eval_dataloader: torch.utils.data.DataLoader) -> Dict:
        device = model_.device
        model = model_.module

        model.eval()
        total_predictions = correct_predictions = 0
        losses = 0
        pos_score = neg_score = 0

        for step, data in enumerate(tqdm.tqdm(eval_dataloader)):
            pair_lens = torch.tensor(data.metadata["seqlens"], dtype=torch.int32, device=model.device)
            data = recursive_apply(data, lambda x: x.to(device))

            packed_input_ids: torch.Tensor = data["packed_input_ids"]
            neg_input_lens = pair_lens - data["pos_input_lens"]
            assert (neg_input_lens > 0).all()
            input_lens = torch.stack([data["pos_input_lens"], neg_input_lens], 1).view(-1)
            group_factor: torch.Tensor = data["group_factor"]
            cu_seqlens = torch.cat([input_lens.new_zeros(1), input_lens.cumsum(0)], 0).int()
            max_seqlen = int(max(cu_seqlens[1:] - cu_seqlens[:-1]))

            if isinstance(model, DeepSpeedPipelineEngine):
                loss_fn_kwargs = dict(
                    input_lens=pair_lens,
                    group_factor=data["group_factor"],
                )
                loss, stats = model.eval_batch(
                    seqlens_cpu=data.metadata["seqlens"],
                    packed_input_ids=packed_input_ids,
                    cu_seqlens=cu_seqlens,
                    loss_fn=_paired_rw_loss_from_model_outputs,
                    input_lens_for_partition=pair_lens,
                    **loss_fn_kwargs,
                )
            else:
                scores: torch.FloatTensor = model(
                    packed_input_ids=packed_input_ids,
                    cu_seqlens=cu_seqlens,
                    max_seqlen=max_seqlen,
                ).logits
                loss, stats = _paired_rw_loss_from_model_outputs(scores, packed_input_ids, cu_seqlens,
                                                                 group_factor)

            if stats is not None:
                assert input_lens.shape[0] % 2 == 0
                losses += loss.item() * (input_lens.shape[0] // 2)
                correct_predictions += stats["correct_predictions"].item()
                total_predictions += input_lens.shape[0] // 2
                pos_score += stats["avg_pos_score"].item()
                neg_score += stats["avg_neg_score"].item()

        if total_predictions > 0:
            return dict(
                loss=float(losses / total_predictions),
                acc=correct_predictions / total_predictions,
                pos_score=float(pos_score / total_predictions),
                neg_score=float(neg_score / total_predictions),
            )
        return dict()


model_api.register_interface("paired_rw", PairedRewardInterface)
