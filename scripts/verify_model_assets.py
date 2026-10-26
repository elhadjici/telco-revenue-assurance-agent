"""Verify copied assets and Helm integration without importing or running pipelines."""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "model-assets-verification"
MODELS = (
    ("churn", "churn", "lightgbmRuntimeName"),
    ("fraud", "rafm", "onnxRuntimeName"),
    ("qoe", "starlink-qoe", "qoeRuntimeName"),
)
PIPELINES = (
    "pipeline/01-telco-revass-randomforest-pipeline.py",
    "pipeline/03-telco-churn-kfp-pipeline.py",
    "pipeline/starlink_qoe_pipeline.py",
    "pipeline/starlink_qoe_pipeline.yaml",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def source_resource(path: str) -> dict[str, Any]:
    resource = yaml.safe_load((ROOT / path).read_text())
    if resource["kind"] == "List":
        require(len(resource["items"]) == 1, f"Expected one source resource in {path}")
        resource = resource["items"][0]
    return resource


def verify_copies() -> None:
    manifest = json.loads((ROOT / "serving/source-manifest.json").read_text())
    expected_paths = set(PIPELINES)
    for _, prefix, _ in MODELS:
        for kind in ("servingruntime", "inferenceservice"):
            expected_paths.add(f"serving/{prefix}-{kind}.yaml")
    entries = manifest["assets"]
    require(
        {entry["path"] for entry in entries} == expected_paths
        and len(entries) == len(expected_paths),
        "Source manifest must list exactly the ten copied model assets",
    )
    for entry in entries:
        path = ROOT / entry["path"]
        data = path.read_bytes()
        require(
            hashlib.sha256(data).hexdigest() == entry["sha256"],
            f"Copied source asset changed: {entry['path']}",
        )
        if path.suffix == ".py":
            ast.parse(data, filename=entry["path"])
        else:
            documents = list(yaml.safe_load_all(data))
            require(all(isinstance(doc, dict) for doc in documents), f"Invalid YAML: {path}")
    sys.stdout.write(
        f"PASS: {len(entries)} source hashes and Python/YAML parses "
        f"(source commit {manifest['source_commit']})\n"
    )


def render(helm: str, uri_overrides: dict[str, str]) -> list[dict[str, Any]]:
    command = [
        helm,
        "template",
        "model-assets",
        str(ROOT / "chart"),
        "--namespace",
        NAMESPACE,
        "--kubeconfig",
        "/dev/null",
        "--show-only",
        "templates/model-serving-runtimes.yaml",
        "--show-only",
        "templates/model-inferenceservices.yaml",
    ]
    for key, uri in uri_overrides.items():
        command.extend(["--set-string", f"modelServing.{key}.uri={uri}"])
    result = subprocess.run(  # noqa: S603 -- local Helm rendering, never pipeline execution
        command, check=True, capture_output=True, text=True, cwd=ROOT
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def verify_rendered(
    documents: list[dict[str, Any]],
    values: dict[str, Any],
    uri_overrides: dict[str, str],
) -> None:
    require(len(documents) == 6, "Expected three ServingRuntimes and three InferenceServices")
    resources = {(doc["kind"], doc["metadata"]["name"]): doc for doc in documents}
    require(len(resources) == 6, "Rendered model resource names must be unique per kind")
    managed_fields = {"uid", "resourceVersion", "creationTimestamp", "generation", "finalizers"}
    for document in documents:
        metadata = document["metadata"]
        require(metadata.get("namespace") == NAMESPACE, "Model escaped the release namespace")
        require(not managed_fields.intersection(metadata), "Exported cluster metadata leaked")

    for key, prefix, runtime_key in MODELS:
        config = values["modelServing"][key]
        runtime_name = values["modelServing"][runtime_key]
        runtime = resources[("ServingRuntime", runtime_name)]
        source_runtime = source_resource(f"serving/{prefix}-servingruntime.yaml")
        require(
            runtime["spec"] == source_runtime["spec"],
            f"{key}: runtime behavior differs from source (env/probes/image/resources/formats)",
        )
        require(
            runtime["metadata"]["annotations"]["opendatahub.io/serving-runtime-scope"]
            == "namespace",
            f"{key}: runtime must be namespace-scoped",
        )

        service = resources[("InferenceService", config["serviceName"])]
        source_service = source_resource(f"serving/{prefix}-inferenceservice.yaml")
        annotations = service["metadata"]["annotations"]
        require(
            annotations["serving.kserve.io/deploymentMode"]
            == source_service["metadata"]["annotations"]["serving.kserve.io/deploymentMode"],
            f"{key}: source deployment mode changed",
        )
        require(
            annotations["security.opendatahub.io/enable-auth"] == "true"
            and service["metadata"]["labels"]["networking.kserve.io/visibility"] == "cluster-local",
            f"{key}: model must retain authenticated, internal access",
        )
        require(
            annotations["serving.kserve.io/storageSecretName"] == config["storageSecretName"],
            f"{key}: configured storage Secret is missing",
        )

        expected = copy.deepcopy(source_service["spec"]["predictor"])
        expected["serviceAccountName"] = config["serviceAccountName"]
        model = expected["model"]
        model["name"] = config["modelName"]
        model["protocolVersion"] = "v2"
        model["runtime"] = runtime_name
        if key in uri_overrides:
            del model["storage"]
            model["storageUri"] = uri_overrides[key]
            require(
                "opendatahub.io/connections" not in annotations
                and "opendatahub.io/connection-path" not in annotations,
                f"{key}: data connection annotations conflict with explicit URI",
            )
        else:
            model["storage"]["key"] = config["storageSecretName"]
            require(
                annotations["opendatahub.io/connections"] == config["storageSecretName"]
                and annotations["opendatahub.io/connection-path"] == model["storage"]["path"],
                f"{key}: data connection must retain the source artifact prefix",
            )
        require(
            service["spec"]["predictor"] == expected,
            f"{key}: predictor differs from source beyond approved identity/storage changes",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helm", default="helm", help="Local Helm executable")
    args = parser.parse_args()
    verify_copies()
    values = yaml.safe_load((ROOT / "chart/values.yaml").read_text())
    verify_rendered(render(args.helm, {}), values, {})
    sys.stdout.write("PASS: source runtime/predictor behavior with default data connections\n")
    overrides = {
        key: f"s3://verification-only/{values['modelServing'][key]['storagePath']}"
        for key, _, _ in MODELS
    }
    verify_rendered(render(args.helm, overrides), values, overrides)
    sys.stdout.write(
        "PASS: explicit URI overrides, namespace isolation, and internal authentication\n"
    )
    sys.stdout.write(
        "No pipelines executed; no cluster contact; trained artifacts remain external.\n"
    )


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        ValueError,
        KeyError,
        SyntaxError,
        yaml.YAMLError,
        subprocess.CalledProcessError,
    ) as error:
        sys.stderr.write(f"FAIL: {error}\n")
        if isinstance(error, subprocess.CalledProcessError):
            sys.stderr.write(error.stderr or "")
        raise SystemExit(1) from error
