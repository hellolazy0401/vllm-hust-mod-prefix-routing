import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("serve_prefix_ab", ROOT / "scripts/serve_prefix_ab.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def profile(name="qwen35-dual"):
    return json.loads((ROOT / f"deployment/configs/{name}.json").read_text())


@pytest.mark.parametrize("arm,kill", [("off", "1"), ("on", "0"), ("kill", "1")])
def test_four_chip_plan_disjoint_replicas_and_utility_disabled(tmp_path, arm, kill):
    plan = launcher.make_plan(profile(), arm, tmp_path / "model", tmp_path / "run")
    assert plan["chips"] == 4 and plan["replicas"] == 2
    assert [n["environment"]["ASCEND_RT_VISIBLE_DEVICES"] for n in plan["servers"]] == ["0,1", "2,3"]
    for node in plan["servers"]:
        env, cmd = node["environment"], node["command"]
        assert env["VLLM_HUST_UTILITY_VICTIM_ENABLE"] == "0"
        assert env["VLLM_HUST_UTILITY_VICTIM_KILL_SWITCH"] == "1"
        assert env["VLLM_HUST_PREFIX_ROUTING_KILL_SWITCH"] == kill
        assert "--enable-prefix-caching" in cmd and "--no-enable-prefix-caching" not in cmd
        assert cmd[cmd.index("--tensor-parallel-size")+1] == "2"
        event = json.loads(cmd[cmd.index("--kv-events-config")+1])
        assert event["endpoint"].startswith("ipc://")  # Both native and MOD bind this form.
    routing = json.loads(plan["servers"][0]["environment"]["VLLM_HUST_PREFIX_ROUTING_CONFIG"])
    for node in plan["servers"]:
        assert "--enable-prefix-routing" not in node["command"]
        assert "--prefix-routing-config" not in node["command"]
        assert "--middleware" in node["command"]
    assert routing["nodes"][1]["url"] == plan["servers"][1]["url"]
    assert "REDACTED" in json.dumps(launcher.public_plan(plan))
    assert routing["routing_token"] not in json.dumps(launcher.public_plan(plan))


def test_no_overlapping_device_groups():
    p = profile()
    p["device_groups"] = ["0,1", "1,2"]
    with pytest.raises(ValueError, match="share devices"):
        launcher.validate_profile(p)


def test_single_replica_is_explicit_negative_control(tmp_path):
    plan = launcher.make_plan(profile("qwen35-single"), "on", tmp_path, tmp_path)
    assert plan["chips"] == 2 and plan["replicas"] == 1 and plan["negative_control"]


def test_tp1_rejects_large_weight_index(tmp_path):
    (tmp_path / "model.safetensors.index.json").write_text(json.dumps({"metadata": {"total_size": 70_000_000_000}}))
    with pytest.raises(RuntimeError, match="exceed"):
        launcher.check_weight_budget(tmp_path, profile("small-dual"))


def test_two_container_groups_have_independent_device_names(tmp_path):
    p = profile("qwen35-two-containers")
    p["hosts"] = ["10.0.0.1", "10.0.0.2"]
    plan = launcher.make_plan(p, "on", tmp_path, tmp_path)
    assert plan["chips"] == 4
    assert plan["servers"][1]["url"] == "http://10.0.0.2:18181"
