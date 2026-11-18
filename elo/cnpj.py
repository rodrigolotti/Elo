"""Validação de CNPJ pelos dígitos verificadores.

Isto confere só o formato. Saber se a empresa existe e está ativa exige
consulta à Receita Federal, que fica fora da V1 (conferência manual).
"""

from __future__ import annotations

import re

_PESOS_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
_PESOS_2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]


def somente_digitos(cnpj: str) -> str:
    return re.sub(r"\D", "", cnpj)


def _digito(numeros: list[int], pesos: list[int]) -> int:
    resto = sum(n * p for n, p in zip(numeros, pesos)) % 11
    return 0 if resto < 2 else 11 - resto


def calcular_digitos(base12: str) -> str:
    """Recebe os 12 primeiros dígitos e devolve os 2 verificadores."""
    numeros = [int(c) for c in base12]
    d1 = _digito(numeros, _PESOS_1)
    d2 = _digito(numeros + [d1], _PESOS_2)
    return f"{d1}{d2}"


def validar_cnpj(cnpj: str) -> bool:
    d = somente_digitos(cnpj)
    if len(d) != 14 or d == d[0] * 14:
        return False
    return d[12:] == calcular_digitos(d[:12])


def formatar_cnpj(cnpj: str) -> str:
    d = somente_digitos(cnpj)
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
