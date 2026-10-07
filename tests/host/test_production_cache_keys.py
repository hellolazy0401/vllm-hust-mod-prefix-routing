# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# Requires the real qualified host. No isolated fixture imports.
import asyncio
from types import SimpleNamespace
from vllm_hust_prefix_routing.components.openai_proxy import PrefixRoutingProxy
from vllm.lora.request import LoRARequest
from vllm.multimodal.inputs import MultiModalFeatureSpec, PlaceholderRange
from vllm.sampling_params import SamplingParams
from vllm.utils.hashing import sha256_cbor
from vllm.v1.core.kv_cache_utils import get_request_block_hasher, init_none_hash
from vllm.v1.engine import EngineCoreRequest
from vllm.v1.request import Request as VllmRequest
def test_prefix_routing_renders_completion_engine_inputs():
    lora_request = LoRARequest("completion-lora", 1, "/tmp/completion-lora")

    class Serving:
        async def render_completion_request(self, request):
            return [
                {
                    "type": "token",
                    "prompt_token_ids": [1, 2],
                    "cache_salt": "salt",
                },
                {"type": "token", "prompt_token_ids": [3]},
            ]

        def _maybe_get_adapters(self, request):
            assert request.model == "test-model"
            return lora_request

    proxy = object.__new__(PrefixRoutingProxy)
    proxy.app_state = SimpleNamespace(openai_serving_completion=Serving())

    rendered = asyncio.run(
        proxy._render_request(
            "/v1/completions",
            {"model": "test-model", "prompt": ["first", "second"]},
        )
    )

    assert rendered == (
        [
            {
                "type": "token",
                "prompt_token_ids": [1, 2],
                "cache_salt": "salt",
            },
            {"type": "token", "prompt_token_ids": [3]},
        ],
        lora_request,
    )

def test_prefix_routing_renders_chat_engine_inputs():
    lora_request = LoRARequest("image", 2, "/tmp/image-lora")

    class Serving:
        async def render_chat_request(self, request):
            return [], [
                {
                    "type": "token",
                    "prompt_token_ids": [4, 5],
                    "cache_salt": "chat-salt",
                }
            ]

        def _maybe_get_adapters(self, request, supports_default_mm_loras=False):
            assert supports_default_mm_loras
            return lora_request

    proxy = object.__new__(PrefixRoutingProxy)
    proxy.app_state = SimpleNamespace(openai_serving_chat=Serving())

    rendered = asyncio.run(
        proxy._render_request(
            "/v1/chat/completions",
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    )

    assert rendered == (
        [
            {
                "type": "token",
                "prompt_token_ids": [4, 5],
                "cache_salt": "chat-salt",
            }
        ],
        lora_request,
    )

def test_prefix_routing_hashes_match_engine_core_cache_key_path():
    init_none_hash(sha256_cbor)
    block_hasher = get_request_block_hasher(8, sha256_cbor)
    engine_input = {"type": "token", "prompt_token_ids": list(range(24))}

    def make_engine_core_request(
        *, mm_identifier: str, lora_name: str, cache_salt: str
    ) -> EngineCoreRequest:
        return EngineCoreRequest(
            request_id="production-request",
            prompt_token_ids=list(range(24)),
            mm_features=[
                MultiModalFeatureSpec(
                    data=None,
                    modality="image",
                    identifier=mm_identifier,
                    mm_position=PlaceholderRange(offset=8, length=8),
                    mm_hash="base-mm-hash",
                )
            ],
            sampling_params=SamplingParams(max_tokens=1),
            pooling_params=None,
            arrival_time=1.0,
            lora_request=LoRARequest(lora_name, 1, f"/tmp/{lora_name}"),
            cache_salt=cache_salt,
            data_parallel_rank=None,
        )

    class InputProcessor:
        request: EngineCoreRequest

        def process_inputs(self, **kwargs):
            assert kwargs["prompt"] is engine_input
            assert kwargs["supported_tasks"] == ("generate",)
            assert kwargs["lora_request"] is self.request.lora_request
            return self.request

    input_processor = InputProcessor()
    proxy = object.__new__(PrefixRoutingProxy)
    proxy._block_hasher = block_hasher
    proxy.app_state = SimpleNamespace(
        engine_client=SimpleNamespace(input_processor=input_processor)
    )

    routing_hashes = {}
    variants = {
        "base": ("mm-a", "lora-a", "salt-a"),
        "different-mm": ("mm-b", "lora-a", "salt-a"),
        "different-lora": ("mm-a", "lora-b", "salt-a"),
        "different-salt": ("mm-a", "lora-a", "salt-b"),
    }
    for name, (mm_identifier, lora_name, cache_salt) in variants.items():
        input_processor.request = make_engine_core_request(
            mm_identifier=mm_identifier,
            lora_name=lora_name,
            cache_salt=cache_salt,
        )
        routing_request = proxy._make_cache_key_request(
            engine_input,
            input_processor.request.lora_request,
            ("generate",),
        )
        production_request = VllmRequest.from_engine_core_request(
            input_processor.request,
            block_hasher,
        )

        assert routing_request.block_hashes == production_request.block_hashes
        assert routing_request.mm_features == production_request.mm_features
        assert routing_request.lora_request == production_request.lora_request
        assert routing_request.cache_salt == production_request.cache_salt
        routing_hashes[name] = routing_request.block_hashes

    assert routing_hashes["different-mm"] != routing_hashes["base"]
    assert routing_hashes["different-lora"] != routing_hashes["base"]
    assert routing_hashes["different-salt"] != routing_hashes["base"]
