"""Arquivo de recuperação: o mesmo formato no JavaScript do painel e no Python.

Cada caso roda nos dois lados: o arquivo gerado por um abre no outro, e frase
errada ou arquivo adulterado são recusados com a mesma mensagem clara.
"""

import base64
import copy
import json
import re
import shutil
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from conftest import CNPJ_TRANSP
from elo.chaves import ParDeChaves, verificar_assinatura
from elo.api import PASTA_PAINEL
from elo.recuperacao import (
    ITERACOES_PBKDF2, ErroRecuperacao, abrir_arquivo_recuperacao, criar_arquivo_recuperacao,
    gerar_frase_senha, normalizar_frase,
)
from test_painel_js import Navegador, enviar, navegador, org_js  # noqa: F401
from test_api import cliente, plataforma  # noqa: F401  (fixtures)

PASTA_JS = PASTA_PAINEL / "js"
URL_OWASP = "https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html"
FRASE_ERRADA = "ABCDE-FGHJK-MNPQR-STVWX-YZ012"
ABRIR_FALHOU = "não foi possível abrir o arquivo: a frase-senha está errada ou o arquivo foi alterado"


@pytest.fixture(scope="module")
def arquivo_js():
    """Um arquivo gerado pelo JavaScript, reaproveitado nos casos de adulteração."""
    if shutil.which("node") is None:
        pytest.skip("Node não encontrado")
    nav = Navegador()
    try:
        r = nav("recuperacao_criar", cnpj=CNPJ_TRANSP, chave_id="transp#recuperacao1")
        if not r["ok"]:
            pytest.skip(f"este Node não gera o arquivo: {r['erro']}")
        return r
    finally:
        nav.fechar()


def abrir_js(navegador, arquivo, frase, cnpj_esperado=None, nome="rec"):
    args = {"nome": nome, "arquivo": arquivo, "frase": frase}
    if cnpj_esperado is not None:
        args["cnpj_esperado"] = cnpj_esperado
    return navegador("recuperacao_abrir", **args)


# ---------- parâmetros e fonte ----------

def test_iteracoes_seguem_a_owasp_e_a_fonte_esta_citada(arquivo_js):
    assert ITERACOES_PBKDF2 == 600_000
    js = (PASTA_JS / "recuperacao.js").read_text(encoding="utf-8")
    assert "export const ITERACOES_PBKDF2 = 600_000;" in js
    for fonte in (js, (Path(__file__).parents[1] / "elo" / "recuperacao.py").read_text("utf-8")):
        assert URL_OWASP in fonte
        assert '"PBKDF2-HMAC-SHA256: 600,000 iterations"' in fonte
    kdf = arquivo_js["arquivo"]["kdf"]
    assert kdf == {"alg": "PBKDF2-HMAC-SHA256", "iteracoes": 600_000, "sal": kdf["sal"]}
    assert arquivo_js["arquivo"]["cifra"]["alg"] == "AES-256-GCM"


# ---------- frase-senha ----------

def test_frase_gerada_tem_125_bits_em_5_grupos(navegador):
    frases = {navegador("chamar", modulo="recuperacao.js", funcao="gerarFraseSenha",
                        args=[])["valor"] for _ in range(30)}
    frases |= {gerar_frase_senha() for _ in range(30)}
    assert len(frases) == 60
    for f in frases:
        assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{5}(-[0-9A-HJKMNP-TV-Z]{5}){4}", f), f


@pytest.mark.parametrize("digitada", ["abcde fghjk mnpqr stvwx yz012", "ABCDEFGHJKMNPQRSTVWXYZ012",
                                      "ABCDE-FGHJK-MNPQR-STVWX-YZOI2", " abcde-fghjk-mnpqr-stvwx-yzol2 "])
def test_frase_e_normalizada_igual_nos_dois_lados(navegador, digitada):
    js = navegador("chamar", modulo="recuperacao.js", funcao="normalizarFrase", args=[digitada])
    assert js["valor"] == normalizar_frase(digitada) == "ABCDEFGHJKMNPQRSTVWXYZ012"


@pytest.mark.parametrize("digitada", ["", "ABCDE-FGHJK", "ABCDE-FGHJK-MNPQR-STVWX-YZ0123",
                                      "ABCDE-FGHJK-MNPQR-STVWX-YZ01U", "senha123"])
def test_frase_com_formato_errado_e_recusada_com_mensagem(navegador, digitada):
    js = navegador("chamar", modulo="recuperacao.js", funcao="normalizarFrase", args=[digitada])
    with pytest.raises(ErroRecuperacao) as py:
        normalizar_frase(digitada)
    assert not js["ok"] and js["erro"] == f"ErroRecuperacao: {py.value}"
    assert "a frase-senha tem 25 letras e números" in str(py.value)


# ---------- ida e volta entre JS e Python ----------

def test_arquivo_do_js_abre_no_js_com_chave_nao_exportavel(navegador, arquivo_js):
    r = abrir_js(navegador, arquivo_js["arquivo"], arquivo_js["frase"], CNPJ_TRANSP)
    assert r["ok"], r
    assert r["chave_publica"] == arquivo_js["chave_publica"]
    assert r["chave_id"] == "transp#recuperacao1" and r["privada_extraivel"] is False
    assinado = navegador("assinar", nome="rec", objeto={"teste": 1})["assinado"]
    assert verificar_assinatura(assinado, arquivo_js["chave_publica"])


def test_arquivo_do_js_abre_no_python_e_nao_tem_a_chave_em_claro(arquivo_js):
    par = abrir_arquivo_recuperacao(json.dumps(arquivo_js["arquivo"]), arquivo_js["frase"],
                                    CNPJ_TRANSP)
    assert par.publica_b64 == arquivo_js["chave_publica"]
    assert par.chave_id == "transp#recuperacao1"
    assert verificar_assinatura(par.assinar({"teste": 1}), arquivo_js["chave_publica"])

    bruta = par.privada.private_bytes(serialization.Encoding.Raw,
                                      serialization.PrivateFormat.Raw,
                                      serialization.NoEncryption())
    pkcs8 = par.privada.private_bytes(serialization.Encoding.DER,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption())
    texto = json.dumps(arquivo_js["arquivo"])
    for segredo in (bruta, pkcs8):
        for forma in (base64.b64encode(segredo).decode(), segredo.hex(),
                      base64.urlsafe_b64encode(segredo).decode().rstrip("=")):
            assert forma not in texto


def test_arquivo_do_python_abre_no_js(navegador):
    frase = gerar_frase_senha()
    arquivo, par = criar_arquivo_recuperacao(CNPJ_TRANSP, "transp#rec-py", frase)
    r = abrir_js(navegador, arquivo, frase.lower().replace("-", " "))
    assert r["ok"], r
    assert r["chave_publica"] == par.publica_b64 and r["privada_extraivel"] is False
    assinado = navegador("assinar", nome="rec", objeto={"de": "js"})["assinado"]
    assert verificar_assinatura(assinado, par.publica_b64)


# ---------- frase errada, outra organização, adulteração ----------

def recusa_nos_dois_lados(navegador, arquivo, frase, trecho, cnpj_esperado=None):
    js = abrir_js(navegador, arquivo, frase, cnpj_esperado)
    with pytest.raises(ErroRecuperacao) as py:
        abrir_arquivo_recuperacao(arquivo, frase, cnpj_esperado)
    assert not js["ok"], js
    assert js["erro"] == f"ErroRecuperacao: {py.value}", (js["erro"], str(py.value))
    assert trecho in str(py.value)


def test_frase_errada_e_recusada_com_mensagem_clara(navegador, arquivo_js):
    recusa_nos_dois_lados(navegador, arquivo_js["arquivo"], FRASE_ERRADA, ABRIR_FALHOU)


def test_arquivo_de_outra_organizacao_e_recusado(navegador, arquivo_js):
    recusa_nos_dois_lados(navegador, arquivo_js["arquivo"], arquivo_js["frase"],
                          f"é da organização {CNPJ_TRANSP}, não de 99.888.777/0001-00",
                          cnpj_esperado="99.888.777/0001-00")


def _trocar_byte(b64):
    dados = bytearray(base64.b64decode(b64))
    dados[len(dados) // 2] ^= 1
    return base64.b64encode(bytes(dados)).decode()


ADULTERACOES = [
    # o conteúdo ou o cabeçalho autenticado mudou: a abertura falha
    ("dados", lambda a: a["cifra"].update(dados=_trocar_byte(a["cifra"]["dados"])), ABRIR_FALHOU),
    ("iv", lambda a: a["cifra"].update(iv=_trocar_byte(a["cifra"]["iv"])), ABRIR_FALHOU),
    ("sal", lambda a: a["kdf"].update(sal=_trocar_byte(a["kdf"]["sal"])), ABRIR_FALHOU),
    ("cnpj", lambda a: a.update(cnpj="99.888.777/0001-00"), ABRIR_FALHOU),
    ("chave_id", lambda a: a.update(chave_id="outra"), ABRIR_FALHOU),
    ("chave_publica", lambda a: a.update(chave_publica=ParDeChaves.gerar("x").publica_b64),
     ABRIR_FALHOU),
    ("iteracoes-acima", lambda a: a["kdf"].update(iteracoes=600_001), ABRIR_FALHOU),
    ("dados-cortados", lambda a: a["cifra"].update(dados=a["cifra"]["dados"][:24]), ABRIR_FALHOU),
    # estrutura: recusa antes de tentar abrir, dizendo o que está errado
    ("iteracoes-abaixo", lambda a: a["kdf"].update(iteracoes=1000),
     "o arquivo pede 1000 iterações, abaixo do mínimo de 600000"),
    ("formato", lambda a: a.update(formato="outro"),
     "este arquivo não é um arquivo de recuperação do Elo"),
    ("versao", lambda a: a.update(versao=2), "versão 2 do arquivo de recuperação não é suportada"),
    ("kdf-alg", lambda a: a["kdf"].update(alg="PBKDF2-HMAC-SHA1"),
     "arquivo de recuperação com algoritmo desconhecido"),
    ("sal-invalido", lambda a: a["kdf"].update(sal="não é base64!"),
     'arquivo de recuperação corrompido: campo "kdf.sal" inválido'),
    ("iv-tamanho", lambda a: a["cifra"].update(iv="AAAA"),
     'arquivo de recuperação corrompido: campo "cifra.iv" com tamanho errado'),
    ("sem-chave_id", lambda a: a.pop("chave_id"),
     'arquivo de recuperação corrompido: campo "chave_id" ausente'),
]


@pytest.mark.parametrize("mudar, trecho", [(m, t) for _, m, t in ADULTERACOES],
                         ids=[n for n, _, _ in ADULTERACOES])
def test_arquivo_adulterado_e_recusado_nos_dois_lados(navegador, arquivo_js, mudar, trecho):
    arquivo = copy.deepcopy(arquivo_js["arquivo"])
    mudar(arquivo)
    recusa_nos_dois_lados(navegador, arquivo, arquivo_js["frase"], trecho)


def test_texto_que_nao_e_json_e_recusado_no_python():
    with pytest.raises(ErroRecuperacao, match="não é um arquivo de recuperação do Elo"):
        abrir_arquivo_recuperacao("{isto não é json", FRASE_ERRADA)


# ---------- a chave aberta do arquivo troca a chave principal pela API ----------

def test_chave_do_arquivo_assina_a_rotacao_aceita_pela_api(cliente, navegador, org_js):
    criado = navegador("recuperacao_criar", cnpj=CNPJ_TRANSP, chave_id="transp#recuperacao1")
    r = enviar(cliente, navegador, "org", "/organizacoes/recuperacao/rascunho", {
        "cnpj": CNPJ_TRANSP, "chave_id": "transp#recuperacao1",
        "chave_publica": criado["chave_publica"]}, "/organizacoes/recuperacao")
    assert r.status_code == 201, r.text

    # "outro computador": abre o arquivo e gera chaves novas
    assert abrir_js(navegador, criado["arquivo"], criado["frase"], CNPJ_TRANSP,
                    nome="recuperada")["ok"]
    nova = navegador("gerar", nome="org2", chave_id="transp#k2")["chave_publica"]
    nova_rec = navegador("recuperacao_criar", cnpj=CNPJ_TRANSP,
                         chave_id="transp#recuperacao2")["chave_publica"]
    r = enviar(cliente, navegador, "recuperada", "/organizacoes/rotacao/rascunho", {
        "cnpj": CNPJ_TRANSP, "chave_id": "transp#k2", "chave_publica": nova,
        "recuperacao_chave_id": "transp#recuperacao2", "recuperacao_chave_publica": nova_rec,
    }, "/organizacoes/rotacao")
    assert r.status_code == 201, r.text
    assert r.json()["chave_nova"]["chave_publica"] == nova
