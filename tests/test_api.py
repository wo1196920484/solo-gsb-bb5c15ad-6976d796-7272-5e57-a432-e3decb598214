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
