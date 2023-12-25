# Copyright (c) 2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Import libraries
import paddle
from paddle import nn
from typing import Dict, List

# Import from Modulus
from modulus.sym.eq.derivatives import gradient
from modulus.sym.loss.aggregator import Aggregator


class CustomSum(Aggregator):
    """
    Loss aggregation by summation
    """

    def __init__(self, params, num_losses, weights=None):
        super().__init__(params, num_losses, weights)

    def forward(self, losses: Dict[str, paddle.Tensor], step: int) -> paddle.Tensor:
        """
        Aggregates the losses by summation

        Parameters
        ----------
        losses : Dict[str, paddle.Tensor]
            A dictionary of losses
        step : int
            Optimizer step

        Returns
        -------
        loss : paddle.Tensor
            Aggregated loss
        """

        # weigh losses
        losses = self.weigh_losses(losses, self.weights)

        # Initialize loss
        loss: paddle.Tensor = paddle.zeros_like(self.init_loss)

        smoothness = 0.0005  # use 0.0005 to smoothen the transition over ~10k steps
        step_tensor = paddle.to_tensor(step, dtype=paddle.float32)
        decay_weight = (paddle.tanh((20000 - step_tensor) * smoothness) + 1.0) * 0.5

        # Add losses
        for key in losses.keys():
            if "init" not in key:
                loss += (1 - decay_weight) * (losses[key])
            else:
                loss += decay_weight * (losses[key])
        return loss
