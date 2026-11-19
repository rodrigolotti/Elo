"""Modo demonstração: só existe com ELO_DEMO=ligado; o agente simulado emite três ofertas."""

import pytest
from fastapi.testclient import TestClient

from conftest import CNPJ_EMBARC, CNPJ_TRANSP
from elo.api import criar_app
from elo.chaves import ParDeChaves
from elo.mandato import AVISO_PISO
from test_api import EstimadorFalso, LIMITES, assinar_e_enviar, plataforma  # noqa: F401
from test_api_painel import entrar


@pytest.fixture
def demo(plataforma):
    c = TestClient(criar_app(plataforma, estimador=EstimadorFalso(), demo=True))
    chave_org = ParDeChaves.gerar("transp#k1")
    _, r = assinar_e_enviar(c, "/organizacoes/rascunho", {
        "cnpj": CNPJ_TRANSP, "razao_social": "Transportadora Fictícia Ltda",
        "chave_id": chave_org.chave_id, "chave_publica": chave_org.publica_b64,
    }, chave_org, "/organizacoes")
    assert r.status_code == 201
    return {"cliente": c, "chave_org": chave_org, "sessao": entrar(c, chave_org)}


def preparar(demo):
    """O que o painel faz em "Preparar demonstração": a organização assina."""
    c, chave_org = demo["cliente"], demo["chave_org"]
    agente = c.get("/demo/agente").json()
    _, r = assinar_e_enviar(c, "/agentes/rascunho", {
        "cnpj": CNPJ_TRANSP, "nome": agente["nome"], "chave_id": agente["chave_id"],
        "chave_publica": agente["chave_publica"]}, chave_org, "/agentes")
    agente_id = r.json()["agente_id"]
    _, r = assinar_e_enviar(c, "/mandatos/rascunho", {
        "cnpj": CNPJ_TRANSP, "agente_id": agente_id, "limites": LIMITES}, chave_org, "/mandatos")
    assert r.status_code == 201
    return agente_id, r.json()["id"]


def test_desligado_as_rotas_de_demonstracao_nao_existem(plataforma, monkeypatch):
    monkeypatch.delenv("ELO_DEMO", raising=False)
    c = TestClient(criar_app(plataforma, estimador=EstimadorFalso()))
    assert c.get("/demo/agente").status_code == 404
    assert c.post("/demo/simular").status_code == 404
    assert not [p for p in c.get("/openapi.json").json()["paths"] if p.startswith("/demo")]


def test_ligado_pela_variavel_de_ambiente(plataforma, monkeypatch):
    monkeypatch.setenv("ELO_DEMO", "ligado")
    c = TestClient(criar_app(plataforma, estimador=EstimadorFalso()))
    assert c.get("/demo/agente").json()["nome"] == "Agente de demonstração"


def test_simular_exige_sessao_e_preparacao(demo):
    c = demo["cliente"]
    assert c.post("/demo/simular").status_code == 401
    r = c.post("/demo/simular", headers=demo["sessao"])
    assert r.status_code == 400 and "prepare a demonstração" in r.json()["detail"]


def test_simular_emite_tres_ofertas(demo):
    preparar(demo)
    c = demo["cliente"]
    r = c.post("/demo/simular", headers=demo["sessao"]).json()
    assert r["embarcador"]["cnpj"] == CNPJ_EMBARC and r["embarcador"]["cnpj"] != CNPJ_TRANSP
    assert r["piso_centavos"] == 345_720          # 410 km × 6,8284 + 657,56
    dentro, acima, abaixo = r["ofertas"]
    assert (dentro["valor_centavos"], dentro["status"], dentro["violacoes"]) \
        == (435_000, "enviada", [])
    assert acima["status"] == "aguardando_aprovacao" and acima["valor_centavos"] == 900_000
    assert any("acima do limite" in v for v in acima["violacoes"])
    assert abaixo["status"] == "aguardando_aprovacao" and abaixo["valor_centavos"] == 160_000
    assert abaixo["violacoes"][0].startswith(AVISO_PISO)
    # a oferta dentro do mandato vira compromisso válido
    aceite = c.post(f"/ofertas/{dentro['oferta_id']}/aceite", json={
        "token": dentro["token_aceite"], "cnpj": CNPJ_EMBARC, "nome": "Maria",
        "email": "maria@exemplo.com"}).json()
    assert c.get(f"/compromissos/{aceite['id']}/verificacao").json()["valido"]


def test_mandato_revogado_impede_simular(demo):
    _, mandato_id = preparar(demo)
    assinar_e_enviar(demo["cliente"], f"/mandatos/{mandato_id}/revogacao/rascunho", {},
                     demo["chave_org"], f"/mandatos/{mandato_id}/revogacao")
    r = demo["cliente"].post("/demo/simular", headers=demo["sessao"])
    assert r.status_code == 400 and "não tem mandato válido" in r.json()["detail"]
