"""端到端 API 测试。"""

from app.engine import stable_bucket


def test_health(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_create_flag_rejects_bad_weights(client):
    payload = {
        "flag_key": "bad_weights",
        "variants": [{"key": "a", "weight": 60}, {"key": "b", "weight": 30}],
        "rules": [],
    }
    resp = client.post("/api/v1/flags", json=payload)
    assert resp.status_code == 422
    assert "100" in resp.text


def test_create_flag_rejects_duplicate_priority(client):
    payload = {
        "flag_key": "dup_prio",
        "variants": [{"key": "a", "weight": 50}, {"key": "b", "weight": 50}],
        "rules": [
            {"priority": 1, "attribute": "x", "operator": "eq", "values": [1], "variant_key": "a"},
            {"priority": 1, "attribute": "y", "operator": "eq", "values": [2], "variant_key": "b"},
        ],
    }
    resp = client.post("/api/v1/flags", json=payload)
    assert resp.status_code == 422
    assert "priority" in resp.text


def test_create_flag_rejects_rule_unknown_variant(client):
    payload = {
        "flag_key": "ghost_variant",
        "variants": [{"key": "a", "weight": 50}, {"key": "b", "weight": 50}],
        "rules": [
            {"priority": 1, "attribute": "x", "operator": "eq", "values": [1], "variant_key": "c"},
        ],
    }
    resp = client.post("/api/v1/flags", json=payload)
    assert resp.status_code == 422


def test_evaluate_rule_hit(client, simple_flag):
    resp = client.post(
        "/api/v1/flags/btn_color/evaluate",
        json={"user_id": "u1", "context": {"region": "cn"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision_path"] == "rule"
    assert body["variant_key"] == "red"
    assert body["bucket_value"] is None
    assert body["matched_rule"]["attribute"] == "region"
    assert body["config_version"] == 1


def test_evaluate_missing_attribute_falls_back_to_bucket(client, simple_flag):
    resp = client.post(
        "/api/v1/flags/btn_color/evaluate",
        json={"user_id": "u1", "context": {"plan": "free"}},
    )
    body = resp.json()
    assert body["decision_path"] == "bucket"
    assert body["bucket_value"] == stable_bucket("btn_color", "u1")
    assert body["variant_key"] in ("red", "blue")


def test_same_user_same_result_when_config_unchanged(client, simple_flag):
    def call():
        return client.post(
            "/api/v1/flags/btn_color/evaluate",
            json={"user_id": "stable-user", "context": {}},
        ).json()

    first, second = call(), call()
    assert first["variant_key"] == second["variant_key"]
    assert first["bucket_value"] == second["bucket_value"]
    # 但 request_id 不同（两次独立判定）
    assert first["request_id"] != second["request_id"]


def test_explanation_chain_structure(client, simple_flag):
    body = client.post(
        "/api/v1/flags/btn_color/evaluate",
        json={"user_id": "u9", "context": {}},
    ).json()
    steps = [e["step"] for e in body["explanation"]]
    assert steps[0] == "rules_loaded"
    assert "no_rule_matched" in steps
    assert steps[-1] == "variant_selected"
    miss_step = next(e for e in body["explanation"] if e["step"] == "rule_evaluated")
    assert "缺少属性" in miss_step["detail"]["reason"]


def test_snapshot_queryable_by_request_id(client, simple_flag):
    eval_body = client.post(
        "/api/v1/evaluate",
        json={"flag_key": "btn_color", "user_id": "u7", "context": {"region": "cn"}},
    ).json()

    snap = client.get(f"/api/v1/decisions/{eval_body['request_id']}").json()
    assert snap["variant_key"] == "red"
    assert snap["matched_rule_index"] == 0
    assert snap["request_context"] == {"region": "cn"}
    assert snap["flag_key"] == "btn_color"
    assert snap["user_id"] == "u7"


def test_snapshot_not_found(client):
    resp = client.get("/api/v1/decisions/nope")
    assert resp.status_code == 404


def test_idempotent_request_id_replays_snapshot(client, simple_flag):
    payload = {
        "user_id": "u1",
        "context": {"region": "us"},
        "request_id": "fixed-req-001",
    }
    first = client.post("/api/v1/flags/btn_color/evaluate", json=payload).json()
    # 即便上下文改成命中规则，带同一 request_id 重放也必须返回首次结果
    payload["context"] = {"region": "cn"}
    second = client.post("/api/v1/flags/btn_color/evaluate", json=payload).json()
    assert first["request_id"] == second["request_id"] == "fixed-req-001"
    assert first["variant_key"] == second["variant_key"]
    assert first["bucket_value"] == second["bucket_value"]
    assert first["explanation"] == second["explanation"]


def test_request_id_conflict_returns_409(client, simple_flag):
    client.post(
        "/api/v1/flags/btn_color/evaluate",
        json={"user_id": "u1", "request_id": "shared-id"},
    )
    resp = client.post(
        "/api/v1/flags/btn_color/evaluate",
        json={"user_id": "someone-else", "request_id": "shared-id"},
    )
    assert resp.status_code == 409


def test_evaluate_unknown_flag_404(client):
    resp = client.post("/api/v1/evaluate", json={"flag_key": "nope", "user_id": "u1"})
    assert resp.status_code == 404


def test_update_bumps_version(client, simple_flag):
    update = {
        "flag_key": "btn_color",
        "variants": [
            {"key": "red", "weight": 20},
            {"key": "blue", "weight": 80},
        ],
        "rules": [],
    }
    resp = client.post("/api/v1/flags", json=update)
    assert resp.status_code == 201
    assert resp.json()["version"] == 2


def test_three_variants_bucket_distribution(client):
    payload = {
        "flag_key": "tri",
        "variants": [
            {"key": "a", "weight": 10},
            {"key": "b", "weight": 20},
            {"key": "c", "weight": 70},
        ],
        "rules": [],
    }
    client.post("/api/v1/flags", json=payload)
    counts = {"a": 0, "b": 0, "c": 0}
    for i in range(1000):
        body = client.post(
            "/api/v1/flags/tri/evaluate", json={"user_id": f"user-{i}", "context": {}}
        ).json()
        counts[body["variant_key"]] += 1
    # 大样本下接近权重配比，允许较宽误差
    assert 50 < counts["a"] < 160, counts
    assert 140 < counts["b"] < 270, counts
    assert 620 < counts["c"] < 780, counts


def test_flag_crud_lifecycle(client):
    payload = {
        "flag_key": "lifecycle",
        "variants": [{"key": "a", "weight": 100}, {"key": "b", "weight": 0}],
        "rules": [],
    }
    assert client.post("/api/v1/flags", json=payload).status_code == 201
    assert client.get("/api/v1/flags/lifecycle").status_code == 200
    keys = [f["flag_key"] for f in client.get("/api/v1/flags").json()]
    assert "lifecycle" in keys
    assert client.delete("/api/v1/flags/lifecycle").status_code == 204
    assert client.get("/api/v1/flags/lifecycle").status_code == 404


def _versioned_flag(client):
    """创建 v1（region=cn -> a，权重 a/b 各 50），再更新为 v2（region=cn -> b，a=0/b=100）。"""
    v1 = {
        "flag_key": "ver_flag",
        "description": "v1",
        "variants": [{"key": "a", "weight": 50}, {"key": "b", "weight": 50}],
        "rules": [
            {"priority": 1, "attribute": "region", "operator": "eq", "values": ["cn"], "variant_key": "a"}
        ],
    }
    assert client.post("/api/v1/flags", json=v1).status_code == 201
    v2 = {
        "flag_key": "ver_flag",
        "description": "v2",
        "variants": [{"key": "a", "weight": 0}, {"key": "b", "weight": 100}],
        "rules": [
            {"priority": 1, "attribute": "region", "operator": "eq", "values": ["cn"], "variant_key": "b"}
        ],
    }
    assert client.post("/api/v1/flags", json=v2).status_code == 201
    return v1, v2


def test_versions_listed_and_immutable(client):
    v1, v2 = _versioned_flag(client)

    versions = client.get("/api/v1/flags/ver_flag/versions").json()
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["description"] == "v1"
    assert versions[0]["rules"][0]["variant_key"] == "a"
    assert versions[1]["description"] == "v2"
    assert versions[1]["rules"][0]["variant_key"] == "b"

    # 再次更新为 v3 后，历史版本内容保持不变
    v3 = dict(v2, description="v3")
    assert client.post("/api/v1/flags", json=v3).status_code == 201
    versions = client.get("/api/v1/flags/ver_flag/versions").json()
    assert [v["version"] for v in versions] == [1, 2, 3]
    assert versions[0]["description"] == "v1"
    assert versions[0]["rules"][0]["variant_key"] == "a"
    assert versions[0]["variants"][0]["weight"] == 50


def test_get_specific_version(client):
    _versioned_flag(client)
    resp = client.get("/api/v1/flags/ver_flag/versions/1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1
    assert body["rules"][0]["variant_key"] == "a"
    assert {v["key"]: v["weight"] for v in body["variants"]} == {"a": 50, "b": 50}

    assert client.get("/api/v1/flags/ver_flag/versions/99").status_code == 404


def test_versions_unknown_flag_404(client):
    assert client.get("/api/v1/flags/nope/versions").status_code == 404
    assert client.get("/api/v1/flags/nope/versions/1").status_code == 404


def test_evaluate_with_config_version_uses_historical_rules(client):
    _versioned_flag(client)
    ctx = {"region": "cn"}

    latest = client.post(
        "/api/v1/flags/ver_flag/evaluate", json={"user_id": "u1", "context": ctx}
    ).json()
    assert latest["variant_key"] == "b"
    assert latest["config_version"] == 2

    pinned = client.post(
        "/api/v1/flags/ver_flag/evaluate",
        json={"user_id": "u1", "context": ctx, "config_version": 1},
    ).json()
    assert pinned["variant_key"] == "a"
    assert pinned["config_version"] == 1
    assert pinned["matched_rule"]["variant_key"] == "a"

    # 快照记录的也是判定所用版本
    snap = client.get(f"/api/v1/decisions/{pinned['request_id']}").json()
    assert snap["config_version"] == 1


def test_evaluate_with_config_version_uses_historical_weights(client):
    _versioned_flag(client)
    # v1 权重 a/b 各 50，v2 为 a=0/b=100；无规则上下文走分桶
    for i in range(20):
        user = f"bucketer-{i}"
        v1_body = client.post(
            "/api/v1/flags/ver_flag/evaluate",
            json={"user_id": user, "context": {}, "config_version": 1},
        ).json()
        v2_body = client.post(
            "/api/v1/flags/ver_flag/evaluate",
            json={"user_id": user, "context": {}, "config_version": 2},
        ).json()
        assert v2_body["variant_key"] == "b"  # v2 中 a 权重为 0
        assert v1_body["config_version"] == 1
        assert v2_body["config_version"] == 2
        # 同一用户分桶值与版本无关（哈希输入只有 flag_key/user_id）
        assert v1_body["bucket_value"] == v2_body["bucket_value"]


def test_evaluate_with_unknown_config_version_404(client, simple_flag):
    resp = client.post(
        "/api/v1/flags/btn_color/evaluate",
        json={"user_id": "u1", "context": {}, "config_version": 99},
    )
    assert resp.status_code == 404


def test_idempotent_replay_with_config_version(client):
    _versioned_flag(client)
    payload = {
        "user_id": "u1",
        "context": {"region": "cn"},
        "request_id": "ver-req-001",
        "config_version": 1,
    }
    first = client.post("/api/v1/flags/ver_flag/evaluate", json=payload).json()
    assert first["variant_key"] == "a"
    assert first["config_version"] == 1

    # 同一 request_id 重放：即使指定了其他版本，也返回首次结果
    payload["config_version"] = 2
    second = client.post("/api/v1/flags/ver_flag/evaluate", json=payload).json()
    assert second["request_id"] == "ver-req-001"
    assert second["variant_key"] == first["variant_key"]
    assert second["config_version"] == first["config_version"]
    assert second["explanation"] == first["explanation"]


def test_replay_matched_rule_uses_snapshot_version(client):
    _versioned_flag(client)
    body = client.post(
        "/api/v1/flags/ver_flag/evaluate",
        json={"user_id": "u1", "context": {"region": "cn"}, "request_id": "ver-req-002"},
    ).json()
    assert body["matched_rule"]["variant_key"] == "b"  # 当时最新为 v2

    # 更新为 v3（规则指向 a）后重放，matched_rule 仍按快照版本 v2 重建
    v3 = {
        "flag_key": "ver_flag",
        "variants": [{"key": "a", "weight": 50}, {"key": "b", "weight": 50}],
        "rules": [
            {"priority": 1, "attribute": "region", "operator": "eq", "values": ["cn"], "variant_key": "a"}
        ],
    }
    assert client.post("/api/v1/flags", json=v3).status_code == 201
    replay = client.post(
        "/api/v1/flags/ver_flag/evaluate",
        json={"user_id": "u1", "context": {"region": "cn"}, "request_id": "ver-req-002"},
    ).json()
    assert replay["variant_key"] == "b"
    assert replay["config_version"] == 2
    assert replay["matched_rule"]["variant_key"] == "b"


def test_delete_flag_removes_versions(client):
    _versioned_flag(client)
    assert client.delete("/api/v1/flags/ver_flag").status_code == 204
    assert client.get("/api/v1/flags/ver_flag/versions").status_code == 404
    # 同名重建后版本从 1 重新开始，不与历史版本冲突
    payload = {
        "flag_key": "ver_flag",
        "variants": [{"key": "a", "weight": 100}, {"key": "b", "weight": 0}],
        "rules": [],
    }
    resp = client.post("/api/v1/flags", json=payload)
    assert resp.status_code == 201
    assert resp.json()["version"] == 1
    versions = client.get("/api/v1/flags/ver_flag/versions").json()
    assert [v["version"] for v in versions] == [1]
