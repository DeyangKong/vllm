import os
from typing import Dict, List, Optional, Tuple

import torch
import torch_xla.runtime as xr
import torch_xla.core.xla_model as xm

from vllm.attention import get_attn_backend
from vllm.config import (CacheConfig, DeviceConfig, ModelConfig,
                         ParallelConfig, SchedulerConfig, VisionLanguageConfig)
from vllm.logger import init_logger
from vllm.model_executor import set_random_seed
from vllm.sequence import SamplerOutput, SequenceGroupMetadata
from vllm.worker.tpu_model_runner import TPUModelRunner
from vllm.worker.worker_base import LoraNotSupportedWorkerBase
from vllm.utils import get_dtype_size, STR_DTYPE_TO_TORCH_DTYPE

logger = init_logger(__name__)


class TPUWorker(LoraNotSupportedWorkerBase):

    def __init__(
        self,
        model_config: ModelConfig,
        parallel_config: ParallelConfig,
        scheduler_config: SchedulerConfig,
        device_config: DeviceConfig,
        cache_config: CacheConfig,
        vision_language_config: Optional[VisionLanguageConfig],
    ) -> None:
        self.model_config = model_config
        self.parallel_config = parallel_config
        self.scheduler_config = scheduler_config
        self.device_config = device_config
        self.cache_config = cache_config
        self.vision_language_config = vision_language_config
        assert self.device_config.device_type == "tpu"

        if self.cache_config.cache_dtype == "auto":
            self.cache_dtype = self.model_config.dtype
        else:
            self.cache_dtype = STR_DTYPE_TO_TORCH_DTYPE[
                self.cache_config.cache_dtype]

        self.model_runner = TPUModelRunner(
            model_config,
            parallel_config,
            scheduler_config,
            device_config,
            vision_language_config=vision_language_config)
        self.attn_backend = get_attn_backend(self.model_config.dtype)
        self.device = None
        self.tpu_cache = None

    def init_device(self) -> None:
        os.environ["PJRT_DEVICE"] = "TPU"
        self.device = xm.xla_device()
        self.device_config.device = self.device
        torch.set_grad_enabled(False)
        torch.set_default_dtype(self.model_config.dtype)

        # Set random seed.
        # TODO: Set random seed for JAX
        set_random_seed(self.model_config.seed)
        xm.set_rng_state(self.model_config.seed, self.device)

        # Use persistent cache to avoid recompilation.
        xr.initialize_cache(os.path.expanduser("~/.vllm/torch_xla_cache"),
                            readonly=False)

    def load_model(self):
        self.model_runner.load_model()

    def determine_num_available_blocks(self) -> Tuple[int, int]:
        num_tpu_blocks = 2000  # FIXME
        return num_tpu_blocks, 0

    def initialize_cache(
        self,
        num_gpu_blocks: int,
        num_cpu_blocks: int,
    ) -> None:
        self.cache_config.num_gpu_blocks = num_gpu_blocks
        self.cache_config.num_cpu_blocks = num_cpu_blocks
        self.block_size = self.cache_config.block_size

        dtype = self.cache_dtype
        num_layers = self.model_config.get_num_layers(self.parallel_config)
        num_kv_heads = self.model_config.get_num_kv_heads(self.parallel_config)
        head_size = self.model_config.get_head_size()

        self.tpu_cache = []
        tpu_cache_shape = self.attn_backend.get_kv_cache_shape(
            num_gpu_blocks, self.block_size, num_kv_heads, head_size)
        for _ in range(num_layers):
            key_cache = torch.zeros(tpu_cache_shape,
                                    dtype=dtype,
                                    device=self.device)
            value_cache = torch.zeros_like(key_cache)
            self.tpu_cache.append((key_cache, value_cache))
        self.model_runner.block_size = self.block_size
        self._warmup_model()

    def _warmup_model(self) -> None:
        # NOTE(woosuk): Because of buffer donation, the reference to the cache
        # should be updated after the warmup.
        self.model_runner.warmup_model(self.tpu_cache)

    def get_cache_block_size_bytes(self) -> int:
        head_size = self.model_config.get_head_size()
        num_heads = self.model_config.get_num_kv_heads(self.parallel_config)
        num_layers = self.model_config.get_num_layers(self.parallel_config)

        key_cache_block = self.cache_config.block_size * num_heads * head_size
        value_cache_block = key_cache_block
        total = num_layers * (key_cache_block + value_cache_block)
        dtype_size = get_dtype_size(self.cache_dtype)
        return dtype_size * total

    def execute_model(
        self,
        seq_group_metadata_list: Optional[List[SequenceGroupMetadata]] = None,
        blocks_to_swap_in: Optional[Dict[int, int]] = None,
        blocks_to_swap_out: Optional[Dict[int, int]] = None,
        blocks_to_copy: Optional[Dict[int, List[int]]] = None,
    ) -> Optional[SamplerOutput]:
        assert seq_group_metadata_list is not None
        num_seq_groups = len(seq_group_metadata_list)
        assert blocks_to_swap_in is not None
        assert blocks_to_swap_out is not None
        assert blocks_to_copy is not None

        # Currently, TPUWorker does not support swapping.
        # TODO(woosuk): Support block copying.
        assert len(blocks_to_swap_in) == 0
        assert len(blocks_to_swap_out) == 0
        assert len(blocks_to_copy) == 0

        # If there is no input, we don't need to execute the model.
        if num_seq_groups == 0:
            return {}

        output = self.model_runner.execute_model(seq_group_metadata_list,
                                                 self.tpu_cache)
        return output
