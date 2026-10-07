"""Differential decisions against the untouched, SHA-audited PR scheduler."""
import dataclasses
import importlib.util
from pathlib import Path
import random
import sys

from vllm_hust_prefix_routing.components import scheduling_routing as mod


def test_1200_decisions_match_pr173():
    path = Path(__file__).resolve().parents[2] / "evidence/upstream-head/vllm/distributed/prefix_scheduler.py"
    spec = importlib.util.spec_from_file_location("pr173_original_scheduler", path)
    original = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = original
    spec.loader.exec_module(original)
    rng = random.Random(173)
    hashes = [bytes([i]) * 32 for i in range(1, 17)]
    left, right = original.GlobalPrefixScheduler(), mod.GlobalPrefixScheduler()
    for iteration in range(1200):
        node = "node" + str(rng.randrange(4))
        rank = rng.randrange(2)
        count = rng.randrange(17)
        sizes = {0: 16, 1: rng.choice([16, 32, 64])}
        groups = {0: set(hashes[:count]), 1: set(hashes[:rng.randrange(17)])}
        for module, scheduler in ((original, left), (mod, right)):
            scheduler.update_snapshot(module.PrefixCacheSnapshot(
                node_id=node, hash_block_size=16, data_parallel_rank=rank,
                group_block_sizes=sizes, group_hashes=groups))
        prompt = rng.randrange(1, 260)
        limit = rng.choice([None, 0, 16, 48, 128])
        candidates = rng.choice([None, ['node0', 'node2'], ['absent']])
        args = dict(candidate_node_ids=candidates, max_cache_hit_length=limit)
        a, b = left.choose_node(hashes, prompt, **args), right.choose_node(hashes, prompt, **args)
        assert (dataclasses.asdict(a) if a else None) == (dataclasses.asdict(b) if b else None), iteration
