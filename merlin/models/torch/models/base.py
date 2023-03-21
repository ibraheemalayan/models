from typing import List, Optional

import pytorch_lightning as pl
import torch
from torch import nn

from merlin.dataloader.torch import Loader
from merlin.models.torch.core.data import propagate_data_to_children
from merlin.models.torch.loader import sample_batch
from merlin.models.torch.outputs.base import ModelOutput
from merlin.models.torch.utils import module_utils
from merlin.schema import Schema


class Model(pl.LightningModule):
    def __init__(
        self,
        *blocks: nn.Module,
        pre=None,
        post=None,
        schema: Optional[Schema] = None,
        optimizer_cls=torch.optim.Adam,
    ):
        super().__init__()
        self.schema = schema
        self.blocks = nn.ModuleList(blocks)
        self.pre = pre
        self.post = post
        self.optimizer_cls = optimizer_cls

    @propagate_data_to_children
    def forward(self, inputs):
        return module_utils.apply(list(self.to_apply), inputs, targets=None)

    def training_step(self, batch, batch_idx):
        del batch_idx
        inputs, targets = batch

        outputs = self(inputs, targets=targets)

        model_output = self.model_outputs[0]
        loss = model_output.default_loss(outputs, model_output.output)
        self.log("train_loss", loss)

        return loss

    def initialize(self, data: Loader):
        if isinstance(data, Loader):
            self.double()  # TODO: Put in data-loader PR to standardize on float-32
            batch = sample_batch(data, batch_size=1, shuffle=False, include_targets=False)
        else:
            batch = data

        if isinstance(batch, torch.Tensor):
            device = batch.device
        elif isinstance(batch, tuple):
            device = batch[0].device
        elif isinstance(batch, dict):
            for d in batch.values():
                if isinstance(d, torch.Tensor):
                    device = d.device
                    break
        else:
            raise ValueError(f"Unsupported data type {type(batch)}")

        self.to(device)
        return self(batch)

    def configure_optimizers(self):
        return self.optimizer_cls(self.parameters())

    @property
    def model_outputs(self) -> List[ModelOutput]:
        return module_utils.find_all_instances(self, ModelOutput)

    @property
    def to_apply(self):
        if self.pre is not None:
            yield self.pre

        for block in self.blocks:
            yield block

        if self.post is not None:
            yield self.post

    @property
    def first(self) -> nn.Module:
        return self.blocks[0]

    @property
    def last(self) -> nn.Module:
        return self.blocks[-1]
