"""Chaves Ed25519: geração, assinatura e verificação.

Uma assinatura sempre cobre os bytes canônicos do objeto SEM o campo
'assinatura'. Assim qualquer pessoa consegue refazer a conta e conferir.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .canonico import canonicalizar

ALGORITMO = "Ed25519"


def _b64(dados: bytes) -> str:
    return base64.b64encode(dados).decode("ascii")


def _de_b64(texto: str) -> bytes:
    return base64.b64decode(texto.encode("ascii"), validate=True)


@dataclass(frozen=True)
class ParDeChaves:
    chave_id: str
    privada: Ed25519PrivateKey

    @classmethod
    def gerar(cls, chave_id: str) -> "ParDeChaves":
        return cls(chave_id=chave_id, privada=Ed25519PrivateKey.generate())

    @property
    def publica_b64(self) -> str:
        bruta = self.privada.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return _b64(bruta)

    def assinar(self, objeto: dict[str, Any]) -> dict[str, Any]:
        """Devolve uma cópia do objeto com o campo 'assinatura' preenchido."""
        sem_assinatura = {k: v for k, v in objeto.items() if k != "assinatura"}
        assinatura = self.privada.sign(canonicalizar(sem_assinatura))
        return {
            **sem_assinatura,
            "assinatura": {
                "alg": ALGORITMO,
                "chave_id": self.chave_id,
                "valor": _b64(assinatura),
            },
        }


def verificar_assinatura(objeto: dict[str, Any], chave_publica_b64: str) -> bool:
    """Confere se 'assinatura' do objeto foi feita pela chave pública informada."""
    assinatura = objeto.get("assinatura")
    if not isinstance(assinatura, dict) or assinatura.get("alg") != ALGORITMO:
        return False
    sem_assinatura = {k: v for k, v in objeto.items() if k != "assinatura"}
    try:
        publica = Ed25519PublicKey.from_public_bytes(_de_b64(chave_publica_b64))
        publica.verify(_de_b64(assinatura["valor"]), canonicalizar(sem_assinatura))
        return True
    except (InvalidSignature, ValueError, KeyError):
        return False


def chave_publica_valida(chave_publica_b64: str) -> bool:
    """Confere se o texto é uma chave pública Ed25519 (32 bytes em base64)."""
    try:
        Ed25519PublicKey.from_public_bytes(_de_b64(chave_publica_b64))
        return True
    except (ValueError, TypeError, AttributeError):
        return False
