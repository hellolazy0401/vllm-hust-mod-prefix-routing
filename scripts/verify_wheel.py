"""Run with python -I after installing the wheel, without vLLM."""
import json
import os
from importlib import metadata, resources

for key in list(os.environ):
    if key.startswith("VLLM_HUST_PREFIX_ROUTING"):
        del os.environ[key]
from vllm_hust_prefix_routing import __version__

manifest = json.loads(resources.files("vllm_hust_prefix_routing.manifests").joinpath(
    "vllm-hust-extension-v0.2.json").read_text(encoding="utf-8"))
assert manifest["extension_version"] == __version__
assert metadata.version("vllm-hust-prefix-routing") == __version__
eps = metadata.distribution("vllm-hust-prefix-routing").entry_points
assert any(e.group == "vllm_hust.extension_bundles" and e.name == manifest["extension_id"] for e in eps)
register = next(e for e in eps if e.group == "vllm.general_plugins").load()
register()
os.environ["VLLM_HUST_PREFIX_ROUTING_ENABLE"] = "invalid"
os.environ["VLLM_HUST_PREFIX_ROUTING_KILL_SWITCH"] = "1"
register()
import sys
assert not any(m in sys.modules for m in ("vllm", "torch", "aiohttp", "msgspec", "zmq"))
print(json.dumps({"wheel": __version__, "default_off": True, "kill_switch": True, "passed": True}))
