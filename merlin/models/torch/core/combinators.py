from functools import reduce
from typing import Callable, Dict, Union

import torch
from torch import nn

from merlin.models.torch.core.aggregation import SumResidual
from merlin.models.torch.core.base import NoOp, TabularBlock
from merlin.models.torch.utils.torch_utils import apply_module


class ParallelBlock(TabularBlock):
    """
    A block that processes inputs in parallel through multiple layers and returns their outputs.

    Parameters
    ----------
    *inputs : Union[nn.Module, Dict[str, nn.Module]]
        Variable length list of PyTorch modules or dictionaries of PyTorch modules.
    pre : Callable, optional
        Preprocessing function to apply on inputs before processing.
    post : Callable, optional
        Postprocessing function to apply on outputs after processing.
    aggregation : Callable, optional
        Aggregation function to apply on outputs.
    """

    def __init__(
        self, *inputs: Union[nn.Module, Dict[str, nn.Module]], pre=None, post=None, aggregation=None
    ):
        super().__init__(pre, post, aggregation)

        if isinstance(inputs, tuple) and len(inputs) == 1 and isinstance(inputs[0], (list, tuple)):
            inputs = inputs[0]

        if all(isinstance(x, dict) for x in inputs):
            self.parallel_dict = reduce(lambda a, b: dict(a, **b), inputs)
        elif all(isinstance(x, nn.Module) for x in inputs):
            self.parallel_dict = {i: m for i, m in enumerate(inputs)}
        else:
            raise ValueError(f"Invalid input. Got: {inputs}")

    def forward(self, inputs, **kwargs):
        """
        Process inputs through the parallel layers.

        Parameters
        ----------
        inputs : Tensor
            Input tensor to process through the parallel layers.
        **kwargs : dict
            Additional keyword arguments for layer processing.

        Returns
        -------
        outputs : dict
            Dictionary containing the outputs of the parallel layers.
        """
        outputs = {}

        for name, module in self.parallel_dict.items():
            module_inputs = inputs  # TODO: Add filtering when adding schema
            out = apply_module(module, module_inputs, **kwargs)
            if not isinstance(out, dict):
                out = {name: out}
            outputs.update(out)

        return outputs


class WithShortcut(ParallelBlock):
    """Parallel block for a shortcut connection.

    This block will apply `module` to it's inputs  outputs the following:
    ```python
    {
        module_output_name: module(inputs),
        shortcut_output_name: inputs
    }
    ```

    Parameters:
    -----------
    module : nn.Module
        The input module.
    aggregation : nn.Module or None, optional
        Optional module that aggregates the dictionary into a single tensor.
        Defaults to None.
    post : nn.Module or None, optional
        Optional module that takes in a dict of tensors and outputs a transformed dict of tensors.
        Defaults to None.
    block_outputs_name : str or None, optional
        The name of the output dictionary of the parallel block.
        Defaults to the name of the input module.
    **kwargs : dict
        Additional keyword arguments to be passed to the superclass ParallelBlock.


    """

    def __init__(
        self,
        module: nn.Module,
        *,
        aggregation=None,
        post=None,
        module_output_name="output",
        shortcut_output_name="shortcut",
        **kwargs,
    ):
        super().__init__(
            {module_output_name: module, shortcut_output_name: NoOp()},
            post=post,
            aggregation=aggregation,
            **kwargs,
        )


class ResidualBlock(WithShortcut):
    """
    Residual block for a shortcut connection with a sum operation and optional activation.

    Parameters
    ----------
    module : nn.Module
        The input module.
    activation : Union[Callable[[torch.Tensor], torch.Tensor], str], optional
        Activation function to be applied after the sum operation.
        It can be a callable or a string representing a standard activation function.
        Defaults to None.
    post : nn.Module or None, optional
        Optional module that takes in a dict of tensors and outputs a transformed dict of tensors.
        Defaults to None.
    **kwargs : dict
        Additional keyword arguments to be passed to the superclass WithShortcut.

    Examples
    --------
    >>> linear = nn.Linear(5, 3)
    >>> residual_block = ResidualBlock(linear, activation=nn.ReLU())

    """

    def __init__(
        self,
        module: nn.Module,
        *,
        activation: Union[Callable[[torch.Tensor], torch.Tensor], str] = None,
        post=None,
        **kwargs,
    ):
        super().__init__(
            module,
            post=post,
            aggregation=SumResidual(activation=activation),
            **kwargs,
        )


class SequentialBlock(nn.Sequential):
    def __init__(self, *args, pre=None, post=None):
        super().__init__(*args)
        self.pre = pre
        self.post = post

    def __call__(self, inputs, *args, **kwargs):
        if self.pre is not None:
            inputs = apply_module(self.pre, inputs, *args, **kwargs)
            outputs = super().__call__(inputs)
        else:
            outputs = super().__call__(inputs, *args, **kwargs)

        if self.post is not None:
            outputs = self.post(outputs)

        return outputs

    def append_with_shortcut(
        self,
        module: nn.Module,
        *,
        post=None,
        aggregation=None,
    ) -> "SequentialBlock":
        return self.append(WithShortcut(module, post=post, aggregation=aggregation))

    def append_with_residual(
        self, module: nn.Module, *, activation=None, **kwargs
    ) -> "SequentialBlock":
        return self.append(ResidualBlock(module, activation=activation, **kwargs))

    def append_branch(
        self,
        *branches: nn.Module,
        post=None,
        aggregation=None,
        **kwargs,
    ) -> "SequentialBlock":
        return self.append(ParallelBlock(*branches, post=post, aggregation=aggregation, **kwargs))

    def repeat(self, num: int = 1) -> "SequentialBlock":
        repeated = [self]
        for _ in range(num):
            repeated.append(self.copy())

        return SequentialBlock(*repeated)

    def repeat_in_parallel(
        self,
        num: int = 1,
        prefix=None,
        names=None,
        post=None,
        aggregation=None,
        copies=True,
        shortcut=False,
        **kwargs,
    ) -> "ParallelBlock":
        repeated = {}
        iterator = names if names else range(num)
        if not names and prefix:
            iterator = [f"{prefix}{num}" for num in iterator]
        for name in iterator:
            repeated[str(name)] = self.copy() if copies else self

        if shortcut:
            repeated["shortcut"] = NoOp()

        return ParallelBlock(repeated, post=post, aggregation=aggregation, **kwargs)
