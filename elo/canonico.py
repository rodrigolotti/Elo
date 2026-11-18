"""Serialização canônica de JSON, subconjunto da RFC 8785 (JCS).

O mesmo objeto precisa virar sempre os mesmos bytes, senão o hash e a
assinatura mudam. Regras aplicadas:

- chaves de objetos ordenadas pela sequência de unidades UTF-16 (como a RFC 8785);
- sem espaços entre elementos;
- strings em UTF-8, escapando só aspas, barra invertida e caracteres de controle;
- números: SÓ inteiros. Números com casa decimal (float) são recusados de
  propósito. O protocolo usa centavos, gramas e litros como inteiros, o que
  elimina erro de arredondamento e a parte mais difícil da RFC 8785.
"""

from __future__ import annotations

import hashlib
from typing import Any

_ESCAPES_CURTOS = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class ErroCanonico(ValueError):
    """Valor que não pode ser serializado de forma canônica."""


def _string(s: str) -> str:
    partes = ['"']
    for ch in s:
        if ch in _ESCAPES_CURTOS:
            partes.append(_ESCAPES_CURTOS[ch])
        elif ord(ch) < 0x20:
            partes.append(f"\\u{ord(ch):04x}")
        else:
            partes.append(ch)
    partes.append('"')
    return "".join(partes)


def _valor(v: Any) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        raise ErroCanonico(
            f"número com casa decimal não é permitido ({v!r}); use inteiro "
            "(centavos, gramas, litros)"
        )
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_valor(x) for x in v) + "]"
    if isinstance(v, dict):
        for k in v:
            if not isinstance(k, str):
                raise ErroCanonico(f"chave de objeto precisa ser texto: {k!r}")
        chaves = sorted(v, key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(_string(k) + ":" + _valor(v[k]) for k in chaves) + "}"
    raise ErroCanonico(f"tipo não suportado: {type(v).__name__}")


def canonicalizar(obj: Any) -> bytes:
    """Retorna os bytes canônicos (UTF-8) de um objeto JSON."""
    return _valor(obj).encode("utf-8")


def hash_sha256(obj: Any) -> str:
    """Hash SHA-256 dos bytes canônicos, no formato 'sha256:<hex>'."""
    return "sha256:" + hashlib.sha256(canonicalizar(obj)).hexdigest()
