from vllm_hust_prefix_routing.components.scheduling_routing import NodePrefixCacheState
from vllm.distributed.kv_events import BlockStored


def stored(group, hashes, kind):
    return BlockStored(block_hashes=hashes, parent_block_hash=None, token_ids=[],
                       block_size=2048, lora_id=None, medium=None, lora_name=None,
                       group_idx=group, kv_cache_spec_kind=kind)


def test_sparse_mamba_checkpoint_with_dense_attention(monkeypatch):
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING_BACKEND", "runtime023-v1")
    hashes = [bytes([i])*32 for i in range(1, 5)]
    state = NodePrefixCacheState("node0", 2048)
    state.apply_events([stored(0, hashes, "full_attention"), stored(1, hashes[-1:], "mamba")])
    assert state.longest_prefix_match(hashes, 8256) == 8192
    assert state.longest_prefix_match(hashes, 8256, max_cache_hit_length=6144) == 0
    state.apply_events([stored(1, hashes[1:2], "mamba")])
    assert state.longest_prefix_match(hashes, 8256, max_cache_hit_length=6144) == 4096


def test_attention_hole_never_counted_as_mamba_hit(monkeypatch):
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING_BACKEND", "runtime023-v1")
    hashes = [bytes([i])*32 for i in range(1, 5)]
    state = NodePrefixCacheState("node0", 2048)
    state.apply_events([stored(0, hashes[:1]+hashes[2:], "full_attention"), stored(1, hashes[-1:], "mamba")])
    assert state.longest_prefix_match(hashes, 8256) == 0


def test_zmq_snapshot_preserves_mamba_metadata():
    import threading
    from vllm_hust_prefix_routing.components.cache_events import ZmqEventPublisher
    p = object.__new__(ZmqEventPublisher)
    p._state_lock = threading.Lock()
    p._group_metadata, p._group_block_sizes, p._group_hashes = {}, {}, {}
    p._data_parallel_rank = 0
    p._apply_to_mirror([stored(1, [b'x'*32], 'mamba')])
    event = p._build_snapshot_batch().events[1]
    assert event.kv_cache_spec_kind == 'mamba'
    assert event.block_hashes == [b'x'*32]
