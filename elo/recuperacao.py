"""Arquivo de recuperação da chave da organização (mesmo formato do painel).

O painel (elo/painel/js/recuperacao.js) gera o arquivo no navegador. Este
módulo lê e grava o mesmo formato, para provar que ele é padrão e para que a
chave de recuperação possa ser aberta sem o painel, se preciso.

    PBKDF2-HMAC-SHA256 (sal de 16 bytes) -> chave AES-256 -> AES-GCM (IV de 12 bytes)
    texto claro: chave privada Ed25519 em PKCS#8 (DER)
    dado autenticado: cabeçalho canônico (CNPJ, chave, parâmetros do KDF)
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .canonico import canonicalizar
from .chaves import ParDeChaves

# OWASP Password Storage Cheat Sheet, seção PBKDF2:
# "PBKDF2-HMAC-SHA256: 600,000 iterations"
# https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
ITERACOES_PBKDF2 = 600_000

FORMATO = "elo-recuperacao"
VERSAO = 1
ALG_KDF = "PBKDF2-HMAC-SHA256"
ALG_CIFRA = "AES-256-GCM"
BYTES_SAL = 16
BYTES_IV = 12
ALFABETO = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
TAMANHO_FRASE = 25


class ErroRecuperacao(ValueError):
    """Arquivo ou frase-senha que não abre a chave de recuperação."""


def gerar_frase_senha() -> str:
    letras = "".join(secrets.choice(ALFABETO) for _ in range(TAMANHO_FRASE))
    return "-".join(letras[i:i + 5] for i in range(0, TAMANHO_FRASE, 5))


def normalizar_frase(texto: str) -> str:
    limpa = "".join(str(texto).upper().split()).replace("-", "")
    limpa = limpa.replace("O", "0").replace("I", "1").replace("L", "1")
    if len(limpa) != TAMANHO_FRASE or any(c not in ALFABETO for c in limpa):
        raise ErroRecuperacao(
            f"a frase-senha tem {TAMANHO_FRASE} letras e números, em 5 grupos "
            "(ex.: 7KQ2M-...); confira o que foi digitado")
    return limpa


def _b64(dados: bytes) -> str:
    return base64.b64encode(dados).decode("ascii")


def _de_b64(texto: Any, campo: str, tamanho: int | None = None) -> bytes:
    try:
        if not isinstance(texto, str):
            raise TypeError
        dados = base64.b64decode(texto.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, TypeError):
        raise ErroRecuperacao(
            f'arquivo de recuperação corrompido: campo "{campo}" inválido') from None
    if tamanho is not None and len(dados) != tamanho:
        raise ErroRecuperacao(
            f'arquivo de recuperação corrompido: campo "{campo}" com tamanho errado')
    return dados


def _cabecalho(arquivo: dict[str, Any]) -> bytes:
    kdf = arquivo["kdf"]
    return canonicalizar({
        "formato": arquivo["formato"], "versao": arquivo["versao"], "cnpj": arquivo["cnpj"],
        "chave_id": arquivo["chave_id"], "chave_publica": arquivo["chave_publica"],
        "kdf": {"alg": kdf["alg"], "iteracoes": kdf["iteracoes"], "sal": kdf["sal"]},
    })


def _chave_da_frase(frase: str, sal: bytes, iteracoes: int) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=sal,
                      iterations=iteracoes).derive(normalizar_frase(frase).encode("ascii"))


def criar_arquivo_recuperacao(cnpj: str, chave_id: str,
                              frase: str) -> tuple[dict[str, Any], ParDeChaves]:
    """Gera a chave de recuperação e o arquivo cifrado. Devolve (arquivo, par)."""
    par = ParDeChaves(chave_id, Ed25519PrivateKey.generate())
    sal, iv = os.urandom(BYTES_SAL), os.urandom(BYTES_IV)
    arquivo: dict[str, Any] = {
        "formato": FORMATO, "versao": VERSAO, "cnpj": cnpj, "chave_id": chave_id,
        "chave_publica": par.publica_b64,
        "kdf": {"alg": ALG_KDF, "iteracoes": ITERACOES_PBKDF2, "sal": _b64(sal)},
        "cifra": {"alg": ALG_CIFRA, "iv": _b64(iv), "dados": ""},
        "criado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    pkcs8 = par.privada.private_bytes(serialization.Encoding.DER,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption())
    chave = _chave_da_frase(frase, sal, ITERACOES_PBKDF2)
    arquivo["cifra"]["dados"] = _b64(AESGCM(chave).encrypt(iv, pkcs8, _cabecalho(arquivo)))
    return arquivo, par


def _conferir_estrutura(arquivo: Any) -> tuple[bytes, bytes, bytes]:
    if not isinstance(arquivo, dict) or arquivo.get("formato") != FORMATO:
        raise ErroRecuperacao("este arquivo não é um arquivo de recuperação do Elo")
    if arquivo.get("versao") != VERSAO:
        raise ErroRecuperacao(
            f"versão {arquivo.get('versao')} do arquivo de recuperação não é suportada")
    for campo in ("cnpj", "chave_id", "chave_publica"):
        if not isinstance(arquivo.get(campo), str) or not arquivo[campo]:
            raise ErroRecuperacao(f'arquivo de recuperação corrompido: campo "{campo}" ausente')
    kdf, cifra = arquivo.get("kdf"), arquivo.get("cifra")
    if not isinstance(kdf, dict) or not isinstance(cifra, dict) \
            or kdf.get("alg") != ALG_KDF or cifra.get("alg") != ALG_CIFRA:
        raise ErroRecuperacao("arquivo de recuperação com algoritmo desconhecido")
    iteracoes = kdf.get("iteracoes")
    if type(iteracoes) is not int or iteracoes < ITERACOES_PBKDF2:
        raise ErroRecuperacao(
            f"o arquivo pede {iteracoes} iterações, abaixo do mínimo de {ITERACOES_PBKDF2}: "
            "foi alterado ou não é confiável")
    _de_b64(arquivo["chave_publica"], "chave_publica", 32)
    return (_de_b64(kdf.get("sal"), "kdf.sal", BYTES_SAL),
            _de_b64(cifra.get("iv"), "cifra.iv", BYTES_IV),
            _de_b64(cifra.get("dados"), "cifra.dados"))


def abrir_arquivo_recuperacao(arquivo: dict[str, Any] | str, frase: str,
                              cnpj_esperado: str | None = None) -> ParDeChaves:
    """Abre o arquivo (objeto ou texto JSON) com a frase-senha."""
    if isinstance(arquivo, str):
        try:
            arquivo = json.loads(arquivo)
        except json.JSONDecodeError:
            raise ErroRecuperacao("este arquivo não é um arquivo de recuperação do Elo") from None
    sal, iv, dados = _conferir_estrutura(arquivo)
    assert isinstance(arquivo, dict)
    if cnpj_esperado is not None and arquivo["cnpj"] != cnpj_esperado:
        raise ErroRecuperacao(f"este arquivo de recuperação é da organização "
                              f"{arquivo['cnpj']}, não de {cnpj_esperado}")
    chave = _chave_da_frase(frase, sal, arquivo["kdf"]["iteracoes"])
    try:
        pkcs8 = AESGCM(chave).decrypt(iv, dados, _cabecalho(arquivo))
    except InvalidTag:
        raise ErroRecuperacao("não foi possível abrir o arquivo: a frase-senha está errada "
                              "ou o arquivo foi alterado") from None
    try:
        privada = serialization.load_der_private_key(pkcs8, password=None)
    except ValueError:
        raise ErroRecuperacao("arquivo de recuperação corrompido: a chave cifrada não é "
                              "Ed25519") from None
    if not isinstance(privada, Ed25519PrivateKey):
        raise ErroRecuperacao("arquivo de recuperação corrompido: a chave cifrada não é Ed25519")
    par = ParDeChaves(arquivo["chave_id"], privada)
    if par.publica_b64 != arquivo["chave_publica"]:
        raise ErroRecuperacao("arquivo adulterado: a chave cifrada não corresponde à chave "
                              "pública declarada")
    return par
