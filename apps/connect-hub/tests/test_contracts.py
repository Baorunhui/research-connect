from connect_hub.contracts import (
    JobErrorCode,
    ModuleManifest,
    SCHEMA_VERSION,
    classify_exception,
)


def test_manifest_requires_connect_job_v1():
    manifest = ModuleManifest(
        module_name="daily-paper",
        module_version="1.2.3",
        supported_job_types=("daily_report",),
    )
    manifest.validate()
    assert manifest.as_dict()["schema_versions"] == [SCHEMA_VERSION]


def test_exception_classification_is_deterministic():
    error = classify_exception(RuntimeError("HTTP 429 Too Many Requests"))
    assert error.code == JobErrorCode.PROVIDER_RATE_LIMITED.value
    assert error.retryable is True
    assert "频率限制" in error.user_message

