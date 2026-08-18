"""T049 — the published contract is the artifact of record.

Asserts the app-generated OpenAPI matches specs/002-extract-audio/
contracts/openapi.yaml on the things that are actually promised: the set of
paths and methods, the status codes each operation can return, the closed
17-value ErrorCode enum, and the single {"error": {"code", "message"}}
envelope on every error response. Drift is fixed at whichever source is
wrong — this test says which.
"""

from pathlib import Path

import pytest
import yaml

from backend.models.errors import ErrorCode

CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "specs"
    / "002-extract-audio"
    / "contracts"
    / "openapi.yaml"
)


@pytest.fixture(scope="module")
def contract() -> dict:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def generated(client) -> dict:
    return client.app.openapi()


def resolve(schema: dict, node):
    """Follow a local $ref so hand-written and generated component names
    (ErrorEnvelope vs. auto-generated) compare on shape, not on naming."""
    seen = 0
    while isinstance(node, dict) and "$ref" in node:
        seen += 1
        assert seen < 10, "runaway $ref chain"
        target = schema
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        node = target
    return node


def operations(document: dict) -> dict[tuple[str, str], dict]:
    return {
        (path, method): operation
        for path, item in document.get("paths", {}).items()
        for method, operation in item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }


def test_paths_and_methods_match(contract, generated):
    assert set(operations(generated)) == set(operations(contract))


def test_status_codes_match_per_operation(contract, generated):
    contract_ops = operations(contract)
    drift = {}
    for key, operation in operations(generated).items():
        expected = set(contract_ops[key].get("responses", {}))
        actual = set(operation.get("responses", {}))
        if expected != actual:
            drift[key] = {"contract": sorted(expected), "app": sorted(actual)}
    assert drift == {}, f"status-code drift: {drift}"


def test_error_code_enum_is_closed_and_identical(contract, generated):
    contract_codes = contract["components"]["schemas"]["ErrorCode"]["enum"]
    assert len(contract_codes) == 17
    assert set(contract_codes) == {code.value for code in ErrorCode}

    # The app exposes the same closed enum through the envelope it serves.
    envelope = generated["components"]["schemas"]["ErrorEnvelope"]
    detail = resolve(generated, envelope["properties"]["error"])
    app_codes = resolve(generated, detail["properties"]["code"])["enum"]
    assert set(app_codes) == set(contract_codes)


def test_every_error_response_uses_the_one_envelope_shape(contract, generated):
    error_statuses = {"400", "404", "409", "429", "500", "503", "504"}
    checked = 0

    for document in (contract, generated):
        for (path, method), operation in operations(document).items():
            for status, response in operation.get("responses", {}).items():
                if str(status) not in error_statuses:
                    continue
                response = resolve(document, response)
                schema = resolve(
                    document,
                    response["content"]["application/json"]["schema"],
                )
                assert set(schema["properties"]) == {"error"}, (path, method, status)
                detail = resolve(document, schema["properties"]["error"])
                assert set(detail["properties"]) == {"code", "message"}, (
                    path,
                    method,
                    status,
                )
                assert set(detail.get("required", [])) == {"code", "message"}
                checked += 1

    assert checked > 0


def test_success_payload_shapes_match(contract, generated):
    """Every response model the contract publishes has the same required
    field set in the app."""
    for name in (
        "InfoResponse",
        "JobAcceptedResponse",
        "JobStatusResponse",
        "HealthResponse",
    ):
        expected = set(contract["components"]["schemas"][name].get("required", []))
        actual = set(generated["components"]["schemas"][name].get("required", []))
        assert expected == actual, f"{name} required-field drift"


def test_file_endpoint_serves_audio_mpeg(contract, generated):
    key = ("/api/jobs/{job_id}/file", "get")
    for document in (contract, generated):
        content = document["paths"][key[0]][key[1]]["responses"]["200"]["content"]
        assert "audio/mpeg" in content


def test_job_create_request_shape_matches(contract, generated):
    expected = contract["components"]["schemas"]["JobCreateRequest"]
    actual = generated["components"]["schemas"]["JobCreateRequest"]

    assert set(expected.get("required", [])) == set(actual.get("required", []))
    assert set(expected["properties"]) == set(actual["properties"])
    # bitrate_kbps is deliberately a plain integer, not an enum: the route
    # refuses out-of-set values with the distinct INVALID_BITRATE code.
    assert actual["properties"]["bitrate_kbps"]["type"] == "integer"
    assert "enum" not in actual["properties"]["bitrate_kbps"]
    assert actual["properties"]["bitrate_kbps"]["default"] == 192
