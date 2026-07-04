import base64

import pytest

from yandex_station.protobuf_parser import Protobuf


@pytest.fixture
def protobuf():
    return Protobuf()


def test_loads_varint(protobuf):
    assert protobuf.loads(b"\x08\x2a") == {1: 42}


def test_loads_multibyte_varint(protobuf):
    # 300 в varint кодируется двумя байтами: 0xac 0x02
    assert protobuf.loads(b"\x08\xac\x02") == {1: 300}


def test_loads_repeated_field_becomes_list(protobuf):
    assert protobuf.loads(b"\x08\x01\x08\x02") == {1: [1, 2]}


def test_loads_accepts_base64_string(protobuf):
    raw = base64.b64encode(b"\x08\x2a").decode()
    assert protobuf.loads(raw) == {1: 42}


def test_dumps_string_field(protobuf):
    assert protobuf.dumps({2: "hi"}) == b"\x12\x02hi"


def test_dumps_rejects_unsupported_value_type(protobuf):
    with pytest.raises(NotImplementedError):
        protobuf.dumps({1: 42})


def test_dumps_loads_roundtrip(protobuf):
    data = protobuf.dumps({1: "x"})
    assert protobuf.loads(data) == {1: b"x"}
