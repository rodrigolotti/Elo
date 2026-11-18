"""Compatibilidade entre o JavaScript do painel e o Python do servidor.

Roda os módulos de elo/painel/js no Node (que tem a mesma Web Crypto do
navegador) e prova que:
- a canonicalização gera os mesmos bytes nos dois lados;
- uma assinatura Ed25519 feita em JavaScript é aceita pelo Python e pela API;
- a chave privada gerada no "navegador" não é exportável.

Sem Node 20+ (com Ed25519 na Web Crypto), estes testes são pulados.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import CNPJ_EMBARC, CNPJ_TRANSP
from elo.canonico import ErroCanonico, canonicalizar, hash_sha256
from elo.chaves import verificar_assinatura
from test_api import CARGA, LIMITES, VALIDADE, cliente, cond, plataforma  # noqa: F401

NAVEGADOR = Path(__file__).parent / "js" / "navegador_node.mjs"


class Navegador:
    """Processo Node que responde a um comando JSON por linha."""

    def __init__(self) -> None:
        self.processo = subprocess.Popen(
            [shutil.which("node"), str(NAVEGADOR)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8")

    def __call__(self, op: str, **args):
        # ensure_ascii: surrogates soltos e não-ASCII viajam como \uXXXX
        self.processo.stdin.write(json.dumps({"op": op, **args}, ensure_ascii=True) + "\n")
        self.processo.stdin.flush()
        linha = self.processo.stdout.readline()
        if not linha:
            raise RuntimeError("o Node encerrou: " + self.processo.stderr.read())
        return json.loads(linha)

    def fechar(self) -> None:
        self.processo.stdin.close()
        self.processo.wait(timeout=10)


@pytest.fixture
def navegador():
    if shutil.which("node") is None:
        pytest.skip("Node não encontrado")
    nav = Navegador()
    if not nav("gerar", nome="sonda", chave_id="sonda")["ok"]:
        nav.fechar()
        pytest.skip("este Node não tem Ed25519 na Web Crypto")
    yield nav
    nav.fechar()


# ---------- canonicalização: mesmos bytes ----------

VETORES = [
    {},
    [],
    "",
    0,
    -1,
    2**53 - 1,
    -(2**53 - 1),
    True,
    False,
    None,
    {"b": 1, "a": 2, "aa": 3, "A": 4, "_": 5, "1": 6, "10": 7, "2": 8},
    # U+1F600 (surrogates D83D DE00) vem ANTES de U+E000 em UTF-16, embora
    # venha depois em code points: é aqui que uma ordenação errada aparece
    {"": 1, "😀": 2, "é": 3, "e": 4, "z": 5, "ü": 6},
    "ação, coração, não, pão",
    "emoji 😀🚚 e CJK 物流",
    "aspas \" barra \\ barra normal / fim",
    "\b\f\n\r\t",
    "\x00\x01\x07\x1f\x7f\x80",
    "  ",
    {"lista": [1, [2, [3, {"z": None, "a": [True, False]}]]], "vazio": {}},
    {"tipo": "oferta", "valor_centavos": 485_000, "inclui": ["pedágio", "seguro"],
     "carga": CARGA, "valida_ate": VALIDADE, "emissor": {"cnpj": CNPJ_TRANSP}},
]


@pytest.mark.parametrize("valor", VETORES, ids=range(len(VETORES)))
def test_canonicalizacao_gera_os_mesmos_bytes(navegador, valor):
    r = navegador("canonico", valor=valor)
    assert r["ok"], r
    assert r["hex"] == canonicalizar(valor).hex()


@pytest.mark.parametrize("valor", VETORES[-3:], ids=range(3))
def test_hash_igual_nos_dois_lados(navegador, valor):
    assert navegador("hash", valor=valor)["hash"] == hash_sha256(valor)


RECUSADOS = [
    1.5,
    {"valor_centavos": 0.1},
    2**53,             # o navegador não representaria sem perda
    -(2**53),
    "\ud800",          # surrogate solto
    {"\udc00": 1},
    ["ok", "\udfff"],
]


@pytest.mark.parametrize("valor", RECUSADOS, ids=range(len(RECUSADOS)))
def test_os_dois_lados_recusam_o_mesmo_tipo_de_valor(navegador, valor):
    r = navegador("canonico", valor=valor)
    assert not r["ok"] and r["erro"].startswith("ErroCanonico"), r
    if isinstance(valor, int) and not isinstance(valor, bool):
        return  # inteiro grande: o Python representa, só o navegador recusa
    with pytest.raises(ErroCanonico):
        canonicalizar(valor)


# ---------- chave e assinatura ----------

def test_chave_privada_do_navegador_nao_e_exportavel(navegador):
    r = navegador("gerar", nome="org", chave_id="transp#k1")
    assert r["ok"] and r["privada_extraivel"] is False
    r = navegador("exportar_privada", nome="org")
    assert not r["ok"] and "InvalidAccessError" in r["erro"]


def test_assinatura_feita_em_javascript_confere_no_python(navegador):
    publica = navegador("gerar", nome="org", chave_id="transp#k1")["chave_publica"]
    for objeto in [v for v in VETORES if isinstance(v, dict)]:
        assinado = navegador("assinar", nome="org", objeto=objeto)["assinado"]
        assert assinado["assinatura"]["alg"] == "Ed25519"
        assert assinado["assinatura"]["chave_id"] == "transp#k1"
        assert verificar_assinatura(assinado, publica)


def test_assinatura_em_javascript_nao_confere_se_alterado_ou_outra_chave(navegador):
    publica = navegador("gerar", nome="org", chave_id="org")["chave_publica"]
    outra = navegador("gerar", nome="outra", chave_id="outra")["chave_publica"]
    assinado = navegador("assinar", nome="org", objeto={"valor_centavos": 485_000})["assinado"]
    assert not verificar_assinatura(assinado, outra)
    assert not verificar_assinatura({**assinado, "valor_centavos": 485_001}, publica)


def test_reassinar_troca_assinatura_antiga(navegador):
    publica = navegador("gerar", nome="org", chave_id="org")["chave_publica"]
    velho = {"a": 1, "assinatura": {"alg": "Ed25519", "chave_id": "x", "valor": "AAAA"}}
    assinado = navegador("assinar", nome="org", objeto=velho)["assinado"]
    assert assinado["assinatura"]["chave_id"] == "org"
    assert verificar_assinatura(assinado, publica)


# ---------- fluxo pela API, com as chaves só no "navegador" ----------

def enviar(cliente, navegador, nome, rota_rascunho, corpo, rota):
    r = cliente.post(rota_rascunho, json=corpo)
    assert r.status_code == 200, r.text
    assinado = navegador("assinar", nome=nome, objeto=r.json()["rascunho"])
    assert assinado["ok"], assinado
    return cliente.post(rota, json=assinado["assinado"])


@pytest.fixture
def org_js(cliente, navegador):
    """Organização, agente e mandato criados com chaves geradas em JavaScript."""
    pub_org = navegador("gerar", nome="org", chave_id="transp#k1")["chave_publica"]
    pub_agente = navegador("gerar", nome="agente", chave_id="agente#k1")["chave_publica"]
    r = enviar(cliente, navegador, "org", "/organizacoes/rascunho", {
        "cnpj": CNPJ_TRANSP, "razao_social": "Transportadora Fictícia Ltda",
        "chave_id": "transp#k1", "chave_publica": pub_org}, "/organizacoes")
    assert r.status_code == 201, r.text
    r = enviar(cliente, navegador, "org", "/agentes/rascunho", {
        "cnpj": CNPJ_TRANSP, "nome": "Agente de cotação",
        "chave_id": "agente#k1", "chave_publica": pub_agente}, "/agentes")
    assert r.status_code == 201, r.text
    agente_id = r.json()["agente_id"]
    r = enviar(cliente, navegador, "org", "/mandatos/rascunho", {
        "cnpj": CNPJ_TRANSP, "agente_id": agente_id, "limites": LIMITES}, "/mandatos")
    assert r.status_code == 201, r.text
    mandato_id = r.json()["id"]
    r = cliente.post("/pedidos", json={"cnpj_solicitante": CNPJ_EMBARC, "canal": "email",
                                       "carga": CARGA})
    assert r.status_code == 201, r.text
    return {"agente_id": agente_id, "mandato_id": mandato_id, "pedido_id": r.json()["id"]}


def test_fluxo_completo_com_assinaturas_do_navegador(cliente, navegador, org_js):
    # oferta acima do limite: o agente assina e ela fica aguardando aprovação
    r = enviar(cliente, navegador, "agente", "/ofertas/rascunho", {
        "pedido_id": org_js["pedido_id"], "agente_id": org_js["agente_id"],
        "mandato_id": org_js["mandato_id"], "condicoes": cond(900_000),
        "valida_ate": VALIDADE}, "/ofertas")
    assert r.status_code == 201, r.text
    emitida = r.json()
    assert emitida["status"] == "aguardando_aprovacao"
    oferta_id = emitida["oferta"]["id"]

    # a organização aprova, assinando no "navegador"
    r = enviar(cliente, navegador, "org", f"/ofertas/{oferta_id}/aprovacao/rascunho",
               {"aprovado_por": "Ana Gestora"}, f"/ofertas/{oferta_id}/aprovacao")
    assert r.status_code == 201, r.text

    r = cliente.post(f"/ofertas/{oferta_id}/aceite", json={
        "cnpj": CNPJ_EMBARC, "nome": "Maria Exemplo", "email": "maria@exemplo.com",
        "token": emitida["token_aceite"]})
    assert r.status_code == 201, r.text
    rel = cliente.get(f"/compromissos/{r.json()['id']}/verificacao").json()
    assert rel["valido"] and all(v["ok"] for v in rel["verificacoes"])


def test_revogacao_assinada_no_navegador(cliente, navegador, org_js):
    mandato_id = org_js["mandato_id"]
    r = enviar(cliente, navegador, "org", f"/mandatos/{mandato_id}/revogacao/rascunho", {},
               f"/mandatos/{mandato_id}/revogacao")
    assert r.status_code == 201, r.text
    r = cliente.post("/ofertas/rascunho", json={
        "pedido_id": org_js["pedido_id"], "agente_id": org_js["agente_id"],
        "mandato_id": mandato_id, "condicoes": cond(), "valida_ate": VALIDADE})
    assert r.json()["status_previsto"] == "aguardando_aprovacao"
    assert any("revogado" in v for v in r.json()["violacoes"])


def test_api_recusa_assinatura_do_navegador_com_chave_errada(cliente, navegador, org_js):
    # o agente tenta assinar um mandato no lugar da organização
    r = enviar(cliente, navegador, "agente", "/mandatos/rascunho", {
        "cnpj": CNPJ_TRANSP, "agente_id": org_js["agente_id"], "limites": LIMITES},
        "/mandatos")
    assert r.status_code == 400
    assert "não confere" in r.json()["detail"]


def test_api_recusa_rascunho_alterado_no_navegador(cliente, navegador, org_js):
    r = cliente.post("/mandatos/rascunho", json={
        "cnpj": CNPJ_TRANSP, "agente_id": org_js["agente_id"], "limites": LIMITES})
    rascunho = r.json()["rascunho"]
    rascunho["limites"]["valor_max_centavos"] = 99_000_000  # alterado antes de assinar
    assinado = navegador("assinar", nome="org", objeto=rascunho)["assinado"]
    r = cliente.post("/mandatos", json=assinado)
    assert r.status_code == 400
    assert "rascunho desconhecido" in r.json()["detail"]


def test_api_recusa_surrogate_solto_com_400(cliente):
    corpo = '{"tipo": "mandato", "x": "\\ud800"}'
    r = cliente.post("/mandatos", content=corpo, headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert "surrogate" in r.json()["detail"]


def test_login_e_recusa_assinados_no_navegador(cliente, navegador, org_js):
    desafio = cliente.post("/sessoes/desafio", json={"cnpj": CNPJ_TRANSP}).json()["desafio"]
    assinado = navegador("assinar", nome="org", objeto=desafio)["assinado"]
    r = cliente.post("/sessoes", json=assinado)
    assert r.status_code == 201, r.text
    sessao = {"Authorization": f"Bearer {r.json()['token']}"}

    r = enviar(cliente, navegador, "agente", "/ofertas/rascunho", {
        "pedido_id": org_js["pedido_id"], "agente_id": org_js["agente_id"],
        "mandato_id": org_js["mandato_id"], "condicoes": cond(900_000),
        "valida_ate": VALIDADE}, "/ofertas")
    oferta_id = r.json()["oferta"]["id"]
    [pendente] = cliente.get("/ofertas", params={"status": "aguardando_aprovacao"},
                             headers=sessao).json()
    assert pendente["oferta"]["id"] == oferta_id

    r = enviar(cliente, navegador, "org", f"/ofertas/{oferta_id}/recusa/rascunho",
               {"recusado_por": "Ana Gestora", "motivo": "acima da tabela — revisar"},
               f"/ofertas/{oferta_id}/recusa")
    assert r.status_code == 201, r.text
    assert cliente.get("/ofertas", headers=sessao).json()[0]["status"] == "recusada"


def test_agente_nao_entra_no_painel_pelo_navegador(cliente, navegador, org_js):
    desafio = cliente.post("/sessoes/desafio", json={"cnpj": CNPJ_TRANSP}).json()["desafio"]
    assinado = navegador("assinar", nome="agente", objeto=desafio)["assinado"]
    assert cliente.post("/sessoes", json=assinado).status_code == 401
