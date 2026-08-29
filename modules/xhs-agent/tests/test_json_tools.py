from xhs_agent.json_tools import extract_json_object


def test_extract_plain_json() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_fenced_json() -> None:
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_prefix() -> None:
    assert extract_json_object('结果如下：{"a": {"b": 2}} thanks') == {"a": {"b": 2}}
