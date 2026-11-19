"""Modo demonstração: só existe com ELO_DEMO=ligado; o agente simulado emite três ofertas."""

import random

import pytest
from fastapi.testclient import TestClient

from conftest import CNPJ_EMBARC, CNPJ_TRANSP
from elo.api import criar_app
from elo.chaves import ParDeChaves
from elo.mandato import AVISO_PISO
from elo.servico import Plataforma
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
    assert c.post("/demo/limpar").status_code == 404
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


PISO = 345_720            # 410 km × 6,8284 + 657,56
LIMITE = LIMITES["valor_max_centavos"]


def simular(demo):
    return demo["cliente"].post("/demo/simular", headers=demo["sessao"]).json()


def test_simular_emite_tres_ofertas(demo):
    preparar(demo)
    c = demo["cliente"]
    r = simular(demo)
    assert r["embarcador"]["cnpj"] == CNPJ_EMBARC and r["embarcador"]["cnpj"] != CNPJ_TRANSP
    assert r["piso_centavos"] == PISO
    dentro, acima, abaixo = r["ofertas"]
    assert dentro["status"] == "enviada" and dentro["violacoes"] == []
    assert acima["status"] == "aguardando_aprovacao"
    assert any("acima do limite" in v for v in acima["violacoes"])
    assert abaixo["status"] == "aguardando_aprovacao"
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


@pytest.mark.parametrize("semente", range(30))
def test_valores_sorteados_ficam_sempre_nas_tres_faixas(demo, semente):
    preparar(demo)
    demo["cliente"].app.state.agente_demo.sorteio = random.Random(semente)
    dentro, acima, abaixo = (o["valor_centavos"] for o in simular(demo)["ofertas"])
    assert PISO <= dentro <= LIMITE
    assert acima > LIMITE
    assert 0 < abaixo < PISO
    assert all(v % 5_000 == 0 for v in (dentro, acima, abaixo))   # múltiplos de R$ 50


def test_cada_pedido_simulado_tem_valores_diferentes(demo):
    preparar(demo)
    demo["cliente"].app.state.agente_demo.sorteio = random.Random(7)
    pedidos = [tuple(o["valor_centavos"] for o in simular(demo)["ofertas"]) for _ in range(3)]
    assert len(set(pedidos)) == 3


def test_limpar_descarta_as_ofertas_em_aberto_da_demonstracao(demo, plataforma):
    preparar(demo)
    c, sessao = demo["cliente"], demo["sessao"]
    primeira = simular(demo)
    dentro = primeira["ofertas"][0]
    aceite = c.post(f"/ofertas/{dentro['oferta_id']}/aceite", json={
        "token": dentro["token_aceite"], "cnpj": CNPJ_EMBARC, "nome": "Maria",
        "email": "maria@exemplo.com"}).json()
    segunda = simular(demo)
    assert c.get("/visao-geral", headers=sessao).json()["barradas_piso"]["quantidade"] == 2

    r = c.post("/demo/limpar", headers=sessao)
    assert r.status_code == 200 and r.json() == {"descartadas": 5}   # 2 + 3 em aberto
    assert c.get("/ofertas", params={"status": "aguardando_aprovacao"}, headers=sessao).json() == []
    visao = c.get("/visao-geral", headers=sessao).json()
    assert visao["aguardando"]["quantidade"] == 0 and visao["barradas_piso"]["quantidade"] == 0
    assert visao["fechados"]["quantidade"] == 1          # o frete aceito não é tocado
    assert c.get(f"/compromissos/{aceite['id']}/verificacao").json()["valido"]
    assert [f["situacao"] for f in c.get("/fretes", headers=sessao).json()] == ["fechado"]

    # descartada não volta: não dá para aprovar nem aceitar
    acima = segunda["ofertas"][1]
    r = c.post(f"/ofertas/{acima['oferta_id']}/aprovacao/rascunho",
               json={"aprovado_por": "Ana"}, headers=sessao)
    assert r.status_code == 400
    dentro2 = segunda["ofertas"][0]
    r = c.post(f"/ofertas/{dentro2['oferta_id']}/aceite", json={
        "token": dentro2["token_aceite"], "cnpj": CNPJ_EMBARC, "nome": "Maria",
        "email": "maria@exemplo.com"})
    assert r.status_code == 400 and "descartada" in r.json()["detail"]

    # nada foi apagado: o registro só cresceu e o estado se reconstrói igual
    assert plataforma.registro.verificar_integridade().integro
    de_novo = Plataforma(plataforma.registro, plataforma.chave, plataforma.relogio)
    assert de_novo.status_oferta == plataforma.status_oferta
    assert set(de_novo.descartadas) == set(plataforma.descartadas)

    # limpar de novo não grava nada; simular de novo funciona sem preparar
    assert c.post("/demo/limpar", headers=sessao).json() == {"descartadas": 0}
    assert len(simular(demo)["ofertas"]) == 3


def test_limpar_exige_sessao_e_sem_preparo_nao_faz_nada(demo):
    c = demo["cliente"]
    assert c.post("/demo/limpar").status_code == 401
    assert c.post("/demo/limpar", headers=demo["sessao"]).json() == {"descartadas": 0}
