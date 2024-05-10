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

from typing import Union, List

import paddle
import logging
from paddle.io import (
    DataLoader,
    BatchSampler,
    RandomSampler,
    DistributedBatchSampler,
    SequenceSampler,
)

from paddle import DataParallel
from typing import Union, List

from modulus.sym.node import Node
from modulus.sym.constants import tf_dt
from modulus.sym.distributed.manager import DistributedManager
from modulus.sym.dataset import Dataset, IterableDataset, DictImportanceSampledPointwiseIterableDataset
from modulus.sym.loss import Loss
from modulus.sym.graph import Graph
from modulus.sym.key import Key
import os

logger = logging.getLogger(__name__)
Tensor = paddle.Tensor


class Constraint:
    """Base class for constraints"""

    def __init__(
        self,
        nodes: List[Node],
        dataset: Union[Dataset, IterableDataset],
        loss: Loss,
        batch_size: int,
        shuffle: bool,
        drop_last: bool,
        num_workers: int,
    ):
        # Get DDP manager
        self.manager = DistributedManager()
        self.place = self.manager.place
        if not drop_last and self.manager.cuda_graphs:
            logger.info("drop_last must be true when using cuda graphs")
            drop_last = True

        # get dataset and dataloader
        self.dataset = dataset
        self.dataloader = iter(
            Constraint.get_dataloader(
                dataset=self.dataset,
                batch_size=batch_size,
                shuffle=shuffle,
                drop_last=drop_last,
                num_workers=num_workers,
            )
        )

        # construct model from nodes
        self.model = Graph(
            nodes,
            Key.convert_list(self.dataset.invar_keys),
            Key.convert_list(self.dataset.outvar_keys),
        )
        # self.model.to(self.place)
        if self.manager.distributed:
            # https://pytorch.org/docs/master/notes/cuda.html#id5
            s = paddle.device.cuda.Stream()
            s.wait_stream(paddle.device.current_stream())
            with paddle.device.cuda.stream_guard(s):
                self.model = DataParallel(
                    self.model,
                    find_unused_parameters=self.manager.find_unused_parameters,
                )
            paddle.device.current_stream().wait_stream(s)
        self._input_names = Key.convert_list(dataset.invar_keys)
        self._output_names = Key.convert_list(dataset.outvar_keys)

        self._input_vars = None
        self._target_vars = None
        self._lambda_weighting = None

        # put loss on device
        self._loss = loss

        # enable dy2st
        import os
        enable_jit = bool(os.getenv("to_static", "True") == "True") # Jit is enabled by default
        enable_cinn = bool(os.getenv("FLAGS_use_cinn", "False") == "True") # CINN is disabled by default
        if enable_jit:
            from paddle import jit
            from paddle import static
            build_strategy = static.BuildStrategy()
            build_strategy.build_cinn_pass = enable_cinn
            self.model.forward = jit.to_static(full_graph=True, build_strategy=build_strategy)(self.model.forward)
            logger.info(f"🍰 🍰 Using jit.to_static with FLAGS_use_cinn={enable_cinn} in Constraint.__init__ in {__file__}, to_static can be disabled by set 'to_static=False python example.py'")
        elif enable_cinn:
            raise RuntimeError(
                f"Please set FLAGS_use_cinn=0 when 'to_static' is set to 0"
            )


    @property
    def input_names(self) -> List[Key]:
        return self._input_names

    @property
    def output_names(self) -> List[Key]:
        return self._output_names

    def load_data(self):
        raise NotImplementedError("Subclass of Constraint needs to implement this")

    def load_data_static(self):
        raise NotImplementedError("Subclass of Constraint needs to implement this")

    def loss(self, step: int):
        raise NotImplementedError("Subclass of Constraint needs to implement this")

    def save_batch(self, filename: str):
        raise NotImplementedError("Subclass of Constraint needs to implement this")

    @staticmethod
    def _set_device(tensor_dict, device=None, requires_grad=False):

        # convert np to torch if needed
        tensor_dict = {
            key: paddle.to_tensor(
                value, dtype=tf_dt, place=device, stop_gradient=not requires_grad
            )
            for key, value in tensor_dict.items()
        }

        # # set requires_grad if needed
        # if requires_grad:
        #     for k, v in tensor_dict.items():
        #         v.stop_gradient = not requires_grad

        return tensor_dict

    @staticmethod
    def get_dataloader(
        dataset: Union[Dataset, IterableDataset],
        batch_size: int,
        shuffle: bool,
        drop_last: bool,
        num_workers: int,
        distributed: bool = None,
        infinite: bool = True,
    ):
        "Return an appropriate dataloader given a dataset"

        assert isinstance(dataset, Dataset) or isinstance(
            dataset, IterableDataset
        ), "error, dataset must be a subclass of Dataset or IterableDataset"
        debug_flag = bool(int(os.getenv("debug", False)))
        if debug_flag:
            shuffle = False
            num_workers = 0
            logger.info("Set shuffle to False and num_workers=0, as debug=1 in os.getenv")

        manager = DistributedManager()

        # use persistent workers
        # this is important for small datasets - torch would otherwise spend a lot of CPU overhead spawning workers each epoch
        persistent_workers = True if num_workers > 0 else False

        # map-style
        if isinstance(dataset, Dataset):

            assert batch_size is not None, "error, batch_size must be specified"
            assert shuffle is not None, "error, shuffle must be specified"
            assert drop_last is not None, "error, drop_last must be specified"

            # if distributed, use distributed sampler
            if distributed is True and manager.distributed:
                batch_sampler = DistributedBatchSampler(
                    dataset,
                    batch_size=batch_size,
                    shuffle=shuffle,
                    drop_last=drop_last,
                )

            # otherwise use standard sampler
            else:
                batch_sampler = BatchSampler(
                    dataset=dataset,
                    shuffle=shuffle,
                    batch_size=batch_size,
                    drop_last=drop_last,
                )
            if dataset.auto_collation:
                # wrap batch_sampler again into BatchSampler for dataset.auto_collation=True
                dataloader = DataLoader(
                    dataset,
                    batch_sampler=BatchSampler(sampler=batch_sampler, batch_size=1),
                    num_workers=num_workers,
                    worker_init_fn=dataset.worker_init_fn,
                    # persistent_workers=persistent_workers,
                    collate_fn=lambda batch: batch[0],
                )

            # otherwise turn on automatic batching in dataloader
            # this passes single indices to the dataset
            # i.e. the dataloader yields default_collate([dataset[i] for i in idx])
            else:
                dataloader = DataLoader(
                    dataset,
                    batch_sampler=batch_sampler,
                    num_workers=num_workers,
                    worker_init_fn=dataset.worker_init_fn,
                    # persistent_workers=persistent_workers,
                )

        # iterable-style
        elif isinstance(dataset, IterableDataset):
            if debug_flag:
                dataloader = dataset
            else:
                if isinstance(dataset, DictImportanceSampledPointwiseIterableDataset):
                    # do not wrap with DataLoader for CUDA computation is not supported in
                    # current paddle's DataLoader
                    # dataloader = dataset
                    if num_workers > 0:
                        logger.warning(
                            f"num_workers({num_workers}) > 0 may cause CUDA error for CUDA"
                            " operation is not supported in current paddle's DataLoader."
                            " Please set num_workers=0 to avoid this error."
                        )
                    dataloader = DataLoader(
                        dataset,
                        batch_size=None,
                        num_workers=num_workers,
                        worker_init_fn=dataset.worker_init_fn,
                        # persistent_workers=persistent_workers,
                    )
                else:
                    # for iterable datasets, must do batching/sampling within dataset
                    dataloader = DataLoader(
                        dataset,
                        batch_size=None,
                        num_workers=num_workers,
                        worker_init_fn=dataset.worker_init_fn,
                        persistent_workers=persistent_workers,
                    )

        # make dataloader infinite
        if infinite:
            dataloader = InfiniteDataLoader(dataloader)

        # initialise dataset if on main thread
        if num_workers == 0:
            dataset.worker_init_fn(0)

        return dataloader


class InfiniteDataLoader:
    "An infinite dataloader, for use with map-style datasets to avoid StopIteration after each epoch"

    def __init__(self, dataloader):
        self.dataloader = dataloader
        self.epoch = 0

    def __iter__(self):
        while True:
            dataloader = iter(self.dataloader)
            for batch in dataloader:
                yield batch
            self.epoch += 1
