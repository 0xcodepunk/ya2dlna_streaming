import base64


class Protobuf:
    """Класс для работы с Protobuf"""

    def __init__(self):
        """Инициализация без параметров для использования в DI контейнере"""
        pass

    def _read(self, raw: bytes, pos: int, length: int) -> tuple[bytes, int]:
        """Читает указанное количество байтов"""
        new_pos = pos + length
        return raw[pos:new_pos], new_pos

    def _read_byte(self, raw: bytes, pos: int) -> tuple[int, int]:
        """Читает один байт"""
        return raw[pos], pos + 1

    def _read_varint(self, raw: bytes, pos: int) -> tuple[int, int]:
        """Читает переменную длину целого числа (varint)"""
        res = 0
        shift = 0
        while True:
            b, pos = self._read_byte(raw, pos)
            res += (b & 0x7F) << shift
            if b & 0x80 == 0:
                break
            shift += 7
        return res, pos

    def _read_bytes(self, raw: bytes, pos: int) -> tuple[bytes, int]:
        """Читает массив байтов (сначала длину, затем данные)"""
        length, pos = self._read_varint(raw, pos)
        data, pos = self._read(raw, pos, length)
        return data, pos

    def _read_dict(self, raw: bytes, pos: int = 0) -> dict:
        """Парсит protobuf данные в словарь"""
        res = {}
        while pos < len(raw):
            b, pos = self._read_varint(raw, pos)
            typ = b & 0b111
            tag = b >> 3

            if typ == 0:  # VARINT
                v, pos = self._read_varint(raw, pos)
            elif typ == 1:  # I64
                v, pos = self._read(raw, pos, 8)
            elif typ == 2:  # LEN
                v, pos = self._read_bytes(raw, pos)
                try:
                    v = self._read_dict(v)
                except Exception:  # noqa
                    pass
            elif typ == 5:  # I32
                v, pos = self._read(raw, pos, 4)
            else:
                raise NotImplementedError

            if tag in res:
                if isinstance(res[tag], list):
                    res[tag] += [v]
                else:
                    res[tag] = [res[tag], v]
            else:
                res[tag] = v

        return res

    def _append_varint(self, b: bytearray, i: int):
        """Добавляет varint в массив байтов"""
        while i >= 0x80:
            b.append(0x80 | (i & 0x7F))
            i >>= 7
        b.append(i)

    def loads(self, raw: str | bytes) -> dict:
        """Разбирает protobuf данные в словарь"""
        if isinstance(raw, str):
            raw = base64.b64decode(raw)
        return self._read_dict(raw)

    def dumps(self, data: dict) -> bytes:
        """Сериализует словарь в protobuf данные"""
        b = bytearray()
        for tag, value in data.items():
            assert isinstance(tag, int)
            if isinstance(value, str):
                b.append(tag << 3 | 2)
                self._append_varint(b, len(value))
                b.extend(value.encode())
            else:
                raise NotImplementedError
        return bytes(b)
