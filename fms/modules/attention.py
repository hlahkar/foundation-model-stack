from typing import Dict, List, Optional, Set, Tuple

import torch
import torch.distributed
from torch import Tensor, nn
from torch.distributed.distributed_c10d import ProcessGroup
from torch.nn import functional as F

from fms import distributed
from fms.distributed.tensorparallel import (
    copy_to_tensor_model_parallel_region,
    reduce_from_tensor_model_parallel_region,
)
from fms.modules.positions import PositionEncoder
from fms.modules.tp import TPModule


class GaudiRotaryEmbedding(torch.nn.Module):
    # This is equivalent Rotary Embedding Class
    # optimized for Intel Gaudi.
    def __init__(self, dim, max_position_embeddings=2048, base=10000, device=None, scaling_factor=1.0):
        super().__init__()

        self.scaling_factor = scaling_factor
        self.dim = dim
        self.max_position_embeddings = max_position_embeddings
        self.base = base
        inv_freq = 1.0 / (self.base ** (torch.arange(0, self.dim, 2).float().to(device) / self.dim))
        self.register_buffer("inv_freq", inv_freq, persistent=False)

        # Build here to make `torch.jit.trace` work.
        self._set_cos_sin_cache(
            seq_len=max_position_embeddings, device=self.inv_freq.device, dtype=torch.get_default_dtype()
        )

    def _set_cos_sin_cache(self, seq_len, device, dtype):
        self.max_seq_len_cached = seq_len
        t = torch.arange(self.max_seq_len_cached, device=device, dtype=self.inv_freq.dtype)

        freqs = torch.outer(t, self.inv_freq)
        # Different from paper, but it uses a different permutation in order to obtain the same calculation
        emb = torch.cat((freqs, freqs), dim=-1)
        self.register_buffer("_cos_cached", emb.cos().to(dtype), persistent=False)
        self.register_buffer("_sin_cached", emb.sin().to(dtype), persistent=False)

    def forward(self, x, seq_len=None):
        # x: [bs, num_attention_heads, seq_len, head_size]
        if seq_len > self.max_seq_len_cached:
            self._set_cos_sin_cache(seq_len=seq_len, device=x.device, dtype=x.dtype)

        return (
            self._cos_cached[:seq_len].to(dtype=x.dtype),
            self._sin_cached[:seq_len].to(dtype=x.dtype),
        )

def apply_gaudi_rope(q, k, cos, sin, position_ids):
    # Fused Rope is a helper method for Rotary Embedding; optimized for Intel Gaudi
    from habana_frameworks.torch.hpex.kernels import RotaryPosEmbeddingHelperV2 as FusedRoPE
    return FusedRoPE.apply(
        q, cos.unsqueeze(0).unsqueeze(0).clone(), sin.unsqueeze(0).unsqueeze(0).clone(), position_ids
    ), FusedRoPE.apply(
        k, cos.unsqueeze(0).unsqueeze(0).clone(), sin.unsqueeze(0).unsqueeze(0).clone(), position_ids
    )


class MultiHeadAttention(nn.Module):
    """
    Performs multi-headed self- or cross-attention, with optional attention masking.
    ...
    Args
    ----
    emb_dim : int
        Latent dimensionality of input and output tensors.
    emb_kq : int
        Latent dimensionality of each head in key and query projections (attention dimension).
    emb_v : int
        Latent dimensionality of each head in value projection (mixing dimension).
    nheads : int
        Number of attention heads.
    p_dropout : float|None
        Dropout probability. Must be in range [0,1]. If 0 or None, dropout will not be used.
    use_bias : bool
        Include bias terms in fully-connected sublayers?
    factorable_emb: Optional[Callable]
        Function that computes factorable embeddings (like RoPE). It is mutually exclusive with
        additive biases on forward() passed as rel_pos_bias
    """

    def __init__(
        self,
        emb_dim,
        emb_kq,
        emb_v,
        nheads,
        kvheads,
        p_dropout=None,
        use_bias=False,
        position_encoder: Optional[PositionEncoder] = None,
    ):
        super(MultiHeadAttention, self).__init__()
        self.nheads = nheads
        self.kvheads = kvheads
        self.emb_dim = emb_dim
        self.emb_kq_per_head = emb_kq
        self.emb_v_per_head = emb_v
        self.p_dropout = p_dropout if p_dropout is not None else 0.0
        self.use_bias = use_bias
        self.query = nn.Linear(
            self.emb_dim, self.nheads * self.emb_kq_per_head, bias=use_bias
        )
        self.key = nn.Linear(
            self.emb_dim, self.kvheads * self.emb_kq_per_head, bias=use_bias
        )
        self.value = nn.Linear(
            self.emb_dim, self.kvheads * self.emb_v_per_head, bias=use_bias
        )
        self.dense = nn.Linear(
            self.nheads * self.emb_v_per_head, self.emb_dim, bias=use_bias
        )
        if self.p_dropout:
            self.attn_dropout = nn.Dropout(self.p_dropout)
        self.position_encoder = position_encoder
        # Avoiding graph breaks
        self.previous_flash: bool = torch.backends.cuda.flash_sdp_enabled()
        self.previous_mem_efficient: bool = (
            torch.backends.cuda.mem_efficient_sdp_enabled()
        )
        self.previous_math: bool = torch.backends.cuda.math_sdp_enabled()
        self.rotary_emb = GaudiRotaryEmbedding(
                self.emb_dim // self.nheads,
                self.emb_dim,
                base=10000.0
            )

    def reset_parameters(self):
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.trunc_normal_(m.weight, mean=0.0, std=0.02)
                if self.use_bias:
                    m.bias.data.zero_()

    def to_tp(self, group: ProcessGroup) -> "TPMultiHeadAttention":
        return TPMultiHeadAttention.import_module(self, group)

    def forward(
        self,
        q,
        k,
        v,
        mask: Optional[Tensor] = None,
        position_ids=None,
        attn_algorithm=None,
        past_key_value_state: Optional[Tuple[Tensor, Tensor]] = None,
        use_cache=False,
        is_self=True,
        is_causal_mask=False,
    ):
        """
        past_key_value_state: tuple
            the cache to be used in attention of the form (<self/cross>_key, <self/cross>_value)
        position_ids: Optional[torch.LongTensor]
            The position of each of the tokens encoded in q and k. Used for RoPE embeddings
        use_cache: bool
            if True, the kv states for self/cross attention will be saved, otherwise they will not be saved
        is_self: bool
            if True, this will perform self attention, otherwise this will perform cross attention. Note: This will
            only be used in the case that use_cache=True. This may be removed in future

        Returns
        -------
        tensor or tuple
            If use_cache=False, only the hidden state will be returned as a tensor. If use_cache=True, a tuple will be
            returned in the form (hidden_state, cache) where hidden_state is a tensor and cache is of the form specified
            in past_key_value_state
        """

        # q, k, v: batch_size x seq_len x emb_dim
        # mask: batch_size x seq_len x seq_len
        batch_size, q_len, _ = q.size()
        kv_len = k.size(1)

        # split emb_dim as nheads*emb_dim_per_head
        # b x h x qlen x ds
        queries = self.query(q).view(
            batch_size, q_len, self.nheads, self.emb_kq_per_head
        )

        if queries.device.type == 'hpu':
            queries = queries.transpose(2, 1)  # / (self.emb_kq_per_head**(1/4))

            # if this is self attention, we always recompute
            # cross attention only gets computed when a cache does not exist
            # if we dont have the cache yet, we need to compute
            # d x (h x ds)
            # b x kvlen x d
            # b x kvlen x h x ds
            # b x h x kvlen x ds
            if is_self or past_key_value_state is None:
                keys = self.key(k).view(
                    batch_size, kv_len, self.kvheads, self.emb_kq_per_head
                )

                values = self.value(v).view(
                    batch_size, kv_len, self.kvheads, self.emb_v_per_head
                )

                keys = keys.transpose(2, 1)  # / (self.emb_kq_per_head**(1/4))
                values = values.transpose(2, 1)  # compatible with QK.T

                # You want to apply rotary embeddings pre-cache
                if self.position_encoder is not None:
                    if position_ids is None:
                        position_ids = torch.arange(
                            0, keys.shape[2], dtype=torch.long, device=queries.device
                        )
                        position_ids = position_ids.reshape(1, keys.shape[2])
                    cos, sin = self.rotary_emb(values, keys.shape[2])
                    queries, keys = apply_gaudi_rope(queries, keys, cos, sin, position_ids)
        else:
            # if this is self attention, we always recompute
            # cross attention only gets computed when a cache does not exist
            # if we dont have the cache yet, we need to compute
            # d x (h x ds)
            # b x kvlen x d
            # b x kvlen x h x ds
            # b x h x kvlen x ds
            if is_self or past_key_value_state is None:
                keys = self.key(k).view(
                    batch_size, kv_len, self.kvheads, self.emb_kq_per_head
                )

                values = self.value(v).view(
                    batch_size, kv_len, self.kvheads, self.emb_v_per_head
                )

                # You want to apply rotary embeddings pre-cache
                if self.position_encoder is not None:
                    queries, keys = self.position_encoder.adjusted_qk(
                        queries, keys, position_ids, past_key_value_state, use_cache
                    )

            queries = queries.transpose(2, 1)  # / (self.emb_kq_per_head**(1/4))
            keys = keys.transpose(2, 1)  # / (self.emb_kq_per_head**(1/4))
            values = values.transpose(2, 1)  # compatible with QK.T


        # if you want to use caching and past_key_value_state is not None meaning you have values in your cache
        if (
            use_cache
            and past_key_value_state is not None
            and past_key_value_state[0].numel() > 0
        ):
            if is_self:
                keys = torch.cat((past_key_value_state[0], keys), dim=2)
                values = torch.cat((past_key_value_state[1], values), dim=2)
            else:
                keys = past_key_value_state[0]
                values = past_key_value_state[1]

        # Merge rel pos bias and mask into single float mask
        if mask is not None:
            # Our expected mask format is bs x q_len x k_len, so to make it broadcastable
            # we need to create the nheads dimension
            while len(mask.size()) != 4:  # expects bs (x nheads) x q_len x kv_len
                mask = mask.unsqueeze(1)

        if self.position_encoder is not None:
            attn_mask: Optional[Tensor] = self.position_encoder.adjusted_mask(
                mask, queries, keys, past_key_value_state, use_cache
            )
        else:
            attn_mask = mask

        # Expand kv so black-box attn will work
        expansion = self.nheads // self.kvheads
        # k/v: b h l d
        if expansion != 1:
            keys_e = keys.unsqueeze(2).expand(-1, -1, expansion, -1, -1).flatten(1, 2)
            values_e = (
                values.unsqueeze(2).expand(-1, -1, expansion, -1, -1).flatten(1, 2)
            )
        else:
            keys_e = keys
            values_e = values

        if queries.device.type == 'hpu':
            # Using FusedSDPA; Intel Gaudi fused implementation of
            # torch.nn.functional.scaled_dot_product_attention
            # for more details please check
            # https://docs.habana.ai/en/latest/PyTorch/Model_Optimization_PyTorch/Optimization_in_PyTorch_Models.html?highlight=FusedSDPA#using-fused-scaled-dot-product-attention-fusedsdpa
            from habana_frameworks.torch.hpex.kernels import FusedSDPA
            import habana_frameworks.torch.hpu as ht
            with ht.sdp_kernel(enable_recompute=True):
                attn = FusedSDPA.apply(queries,
                    keys_e,
                    values_e,
                    attn_mask,
                    self.p_dropout if self.training else 0.0,
                    is_causal_mask,
                    None,
                    'fast'
                )
        else:
            if attn_algorithm:
                # Pick which fused attn kernels will run.
                use_flash = attn_algorithm == "flash"
                use_mem_efficient = attn_algorithm == "mem"
                use_math = attn_algorithm == "math"

                torch.backends.cuda.enable_flash_sdp(use_flash)
                torch.backends.cuda.enable_mem_efficient_sdp(use_mem_efficient)
                torch.backends.cuda.enable_math_sdp(use_math)

            attn = F.scaled_dot_product_attention(
                queries,
                keys_e,
                values_e,
                attn_mask=attn_mask,
                dropout_p=self.p_dropout if self.training else 0.0,
                is_causal=is_causal_mask,
            )

            if attn_algorithm:
                torch.backends.cuda.enable_flash_sdp(self.previous_flash)
                torch.backends.cuda.enable_mem_efficient_sdp(self.previous_mem_efficient)
                torch.backends.cuda.enable_math_sdp(self.previous_math)
        

        # attn: bs x seq_len x nheads*emb_v_per_head
        # attn: b x h x qlen x ds
        # attn after permute: b x qlen x h x ds
        # b x qlen x (d)
        attn = (
            attn.transpose(2, 1)
            .contiguous()
            .view(batch_size, q_len, self.nheads * self.emb_v_per_head)
        )
        out = self.dense(attn)

        # if use_cache=True, we return the hidden_state as well as the kv cache
        if use_cache:
            return out, (keys, values)
        else:
            return out


class TPMultiHeadAttention(MultiHeadAttention, TPModule):
    """
    Performs multi-headed self- or cross-attention, with optional attention masking.
    This subclass adds support for Tensor Parallel
    ...
    Args
    ----
    Check MultiHeadAttention for up-to-date docs

    world_size: int
        the number of processes running this model in TP
    rank: int
        the index of this process wrt to the rest running the model in TP
    """

    def __init__(
        self,
        emb_dim,
        emb_kq,
        emb_v,
        nheads,
        kvheads,
        p_dropout=None,
        use_bias=False,
        position_encoder: Optional[PositionEncoder] = None,
        group: Optional[ProcessGroup] = None,
    ):
        assert torch.distributed.is_initialized()

        rank, world_size = distributed.rank_and_world(group)
        assert (
            nheads % world_size == 0
        ), "The number of heads must be divisible by world size"
        assert (kvheads >= world_size and kvheads % world_size == 0) or (
            kvheads < world_size and world_size % kvheads == 0
        ), "the kv heads must be divisible by the world size or the world size must be divisible by kv heads"
        MultiHeadAttention.__init__(
            self,
            emb_dim,
            emb_kq,
            emb_v,
            nheads // world_size,
            (kvheads // world_size) if kvheads >= world_size else 1,
            p_dropout,
            use_bias,
            position_encoder,
        )
        self.pre_tp_nheads = nheads
        self.pre_tp_kvheads = kvheads
        self.setup_tp(rank, world_size)

    def load_weights(
        self,
        tensor_values: Dict[str, torch.Tensor],
    ):
        # 1. Grab the weights from tensor_values
        used_keys: Set[str] = set()
        query_weight = self._get_sd_weight(
            tensor_values, used_keys, ["query", "weight"]
        )
        key_weight = self._get_sd_weight(tensor_values, used_keys, ["key", "weight"])
        value_weight = self._get_sd_weight(
            tensor_values, used_keys, ["value", "weight"]
        )
        dense_weight = self._get_sd_weight(
            tensor_values, used_keys, ["dense", "weight"]
        )
        if self.use_bias:
            query_bias = self._get_sd_weight(
                tensor_values, used_keys, ["query", "bias"]
            )
            key_bias = self._get_sd_weight(tensor_values, used_keys, ["key", "bias"])
            value_bias = self._get_sd_weight(
                tensor_values, used_keys, ["value", "bias"]
            )
            dense_bias = self._get_sd_weight(
                tensor_values, used_keys, ["dense", "bias"]
            )

        # 2. Raise exceptions
        if len(tensor_values) > (8 if self.use_bias else 4):
            unused_keys = set(tensor_values.keys()).difference(used_keys)
            raise AttributeError(f"Unused weight(s): {', '.join(unused_keys)}")

        # 3. Load and shard the weights
        # The number in max_partition_sizes will signify the largest world size
        # til we need to duplicate.  For instance if we have nheads=16 and
        # world_size=32, then first 2 ranks will get first 1/16th of query
        self.sharded_copy(self.query.weight, query_weight, 0, [self.pre_tp_nheads])
        self.sharded_copy(self.key.weight, key_weight, 0, [self.pre_tp_kvheads])
        self.sharded_copy(self.value.weight, value_weight, 0, [self.pre_tp_kvheads])
        self.sharded_copy(self.dense.weight, dense_weight, 1, [self.world_size])
        if self.use_bias:
            self.sharded_copy(self.query.bias, query_bias, 0, [self.pre_tp_nheads])
            self.sharded_copy(self.key.bias, key_bias, 0, [self.pre_tp_kvheads])
            self.sharded_copy(self.value.bias, value_bias, 0, [self.pre_tp_kvheads])
            self.sharded_copy(self.dense.bias, dense_bias, 1, [self.world_size], False)

    @staticmethod
    def import_module(
        mha: MultiHeadAttention, group: ProcessGroup
    ) -> "TPMultiHeadAttention":
        tp_mha = TPMultiHeadAttention(
            emb_dim=mha.emb_dim,
            emb_kq=mha.emb_kq_per_head,
            emb_v=mha.emb_v_per_head,
            nheads=mha.nheads,
            kvheads=mha.kvheads,
            p_dropout=mha.p_dropout,
            use_bias=mha.use_bias,
            position_encoder=mha.position_encoder,
            group=group,
        )
        return tp_mha

    def forward(
        self,
        q,
        k,
        v,
        mask=None,
        position_ids=None,
        attn_algorithm=None,
        past_key_value_state=None,
        use_cache=False,
        is_self=True,
        is_causal_mask=False,
    ):
        """
        Check MultiHeadAttention for up-to-date arguments and docs
        """

        q_par = copy_to_tensor_model_parallel_region(q)
        k_par = copy_to_tensor_model_parallel_region(k)
        v_par = copy_to_tensor_model_parallel_region(v)
        # rel_pos_bias_par = copy_to_tensor_model_parallel_region(rel_pos_bias)

        out_par = MultiHeadAttention.forward(
            self,
            q_par,
            k_par,
            v_par,
            mask,
            position_ids,
            attn_algorithm,
            past_key_value_state,
            use_cache,
            is_self,
            is_causal_mask,
        )

        # if use_cache=True, we return the hidden_state as well as the kv cache.
        # We only reduce the output, and keep the cache thread-local
        if use_cache:
            out = reduce_from_tensor_model_parallel_region(out_par[0])
            return out, out_par[1]
        else:
            out = reduce_from_tensor_model_parallel_region(out_par)
            return out
