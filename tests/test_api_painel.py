"""API do painel: login por desafio assinado, consultas protegidas e recusa de oferta."""

import pytest
from fastapi.routing import APIRoute

from conftest import CNPJ_TRANSP, cnpj_ficticio
from elo.chaves import ParDeChaves
from test_api import (  # noqa: F401  (fixtures)
    aceitar, api, assinar_e_enviar, cliente, emitir, plataforma,
)

PUBLICAS = {"/verificar/{compromisso_id}", "/compromissos/{compromisso_id}/verificacao",
            "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def desafio(cliente, cnpj=CNPJ_TRANSP):
    r = cliente.post("/sessoes/desafio", json={"cnpj": cnpj})
    assert r.status_code == 200, r.text
    return r.json()["desafio"]


def entrar(cliente, chave, cnpj=CNPJ_TRANSP):
    r = cliente.post("/sessoes", json=chave.assinar(desafio(cliente, cnpj)))
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def sessao(api):
    return entrar(api["cliente"], api["chave_org"])


# ---------- nenhuma consulta aberta ----------

def test_todas_as_consultas_exigem_sessao(cliente):
    consultas = [r.path for r in cliente.app.routes
                 if isinstance(r, APIRoute) and "GET" in r.methods and r.path not in PUBLICAS]
    assert {"/organizacao", "/agentes", "/mandatos", "/ofertas",
            "/compromissos"} <= set(consultas)
    for rota in consultas:
        for cabecalho in ({}, {"Authorization": "Bearer inventado"},
                          {"Authorization": "Basic dXNlcjpzZW5oYQ=="}):
            r = cliente.get(rota, headers=cabecalho)
            assert r.status_code == 401, (rota, cabecalho, r.text)
            assert r.headers["www-authenticate"] == "Bearer"


def test_entrar_com_desafio_assinado(api, sessao):
    d = desafio(api["cliente"])
    assert d["tipo"] == "desafio_sessao" and d["cnpj"] == CNPJ_TRANSP and len(d["nonce"]) >= 40
    r = api["cliente"].get("/organizacao", headers=sessao)
    assert r.status_code == 200 and r.json()["cnpj"] == CNPJ_TRANSP


def test_desafio_vale_uma_vez_so(api):
    assinado = api["chave_org"].assinar(desafio(api["cliente"]))
    assert api["cliente"].post("/sessoes", json=assinado).status_code == 201
    r = api["cliente"].post("/sessoes", json=assinado)
    assert r.status_code == 401 and "já usado" in r.json()["detail"]


def test_tentativa_errada_tambem_consome_o_desafio(api):
    d = desafio(api["cliente"])
    r = api["cliente"].post("/sessoes", json=ParDeChaves.gerar("intrusa").assinar(d))
    assert r.status_code == 401 and "não confere" in r.json()["detail"]
    r = api["cliente"].post("/sessoes", json=api["chave_org"].assinar(d))
    assert r.status_code == 401


def test_chave_do_agente_nao_abre_sessao(api):
    r = api["cliente"].post("/sessoes", json=api["chave_agente"].assinar(desafio(api["cliente"])))
    assert r.status_code == 401


def test_desafio_expira_em_5_minutos(api, relogio):
    d = desafio(api["cliente"])
    relogio.avancar(minutes=5, seconds=1)
    assert api["cliente"].post("/sessoes", json=api["chave_org"].assinar(d)).status_code == 401


def test_desafio_alterado_e_recusado(api):
    d = desafio(api["cliente"])
    d["nonce"] = "escolhido-por-quem-ataca"
    assert api["cliente"].post("/sessoes", json=api["chave_org"].assinar(d)).status_code == 401


def test_sessao_expira_em_30_minutos(api, sessao, relogio):
    relogio.avancar(minutes=29)
    assert api["cliente"].get("/agentes", headers=sessao).status_code == 200
    relogio.avancar(minutes=1, seconds=1)
    assert api["cliente"].get("/agentes", headers=sessao).status_code == 401


def test_sair_encerra_a_sessao(api, sessao):
    assert api["cliente"].delete("/sessoes", headers=sessao).status_code == 204
    assert api["cliente"].get("/agentes", headers=sessao).status_code == 401


def test_desafio_de_organizacao_nao_cadastrada(cliente):
    r = cliente.post("/sessoes/desafio", json={"cnpj": cnpj_ficticio("998887770001")})
    assert r.status_code == 404


def test_desafio_e_rascunho_nao_se_confundem(api):
    c, chave = api["cliente"], api["chave_org"]
    # desafio assinado não serve como ato do protocolo...
    r = c.post("/mandatos", json=chave.assinar(desafio(c)))
    assert r.status_code == 400 and "rascunho desconhecido" in r.json()["detail"]
    # ...e um rascunho assinado não serve para entrar
    rascunho = c.post(f"/mandatos/{api['mandato_id']}/revogacao/rascunho").json()["rascunho"]
    assert c.post("/sessoes", json=chave.assinar(rascunho)).status_code == 401


# ---------- consultas ----------

def test_consultas_mostram_so_a_propria_organizacao(api, sessao):
    c = api["cliente"]
    _, fora = emitir(api, 900_000)
    _, dentro = emitir(api)
    cp_id = aceitar(api, dentro["oferta"]["id"]).json()["id"]

    pendentes = c.get("/ofertas", params={"status": "aguardando_aprovacao"}, headers=sessao).json()
    assert [o["oferta"]["id"] for o in pendentes] == [fora["oferta"]["id"]]
    assert pendentes[0]["violacoes"] and pendentes[0]["recusa"] is None
    assert len(c.get("/ofertas", headers=sessao).json()) == 2
    assert [a["agente_id"] for a in c.get("/agentes", headers=sessao).json()] == [api["agente_id"]]
    assert [m["mandato"]["id"] for m in c.get("/mandatos", headers=sessao).json()] \
        == [api["mandato_id"]]
    [cp] = c.get("/compromissos", headers=sessao).json()
    assert cp["compromisso"]["id"] == cp_id and cp["verificacao"] == f"/verificar/{cp_id}"
    assert c.get(cp["verificacao"]).status_code == 200

    # outra transportadora, com a própria sessão, não vê nada disso
    outra, chave_outra = cnpj_ficticio("998887770001"), ParDeChaves.gerar("outra#k1")
    _, r = assinar_e_enviar(c, "/organizacoes/rascunho", {
        "cnpj": outra, "razao_social": "Outra Transportadora Ltda",
        "chave_id": chave_outra.chave_id, "chave_publica": chave_outra.publica_b64,
    }, chave_outra, "/organizacoes")
    assert r.status_code == 201
    sessao_outra = entrar(c, chave_outra, outra)
    for rota in ("/agentes", "/mandatos", "/ofertas", "/compromissos"):
        assert c.get(rota, headers=sessao_outra).json() == []
    assert c.get("/organizacao", headers=sessao_outra).json()["cnpj"] == outra


def test_status_invalido_na_consulta_vira_422(api, sessao):
    assert api["cliente"].get("/ofertas", params={"status": "qualquer"},
                              headers=sessao).status_code == 422


# ---------- recusa ----------

def recusar(api, oferta_id, chave=None, rota_id=None):
    rota = f"/ofertas/{rota_id or oferta_id}/recusa"
    r = api["cliente"].post(f"/ofertas/{oferta_id}/recusa/rascunho",
                            json={"recusado_por": "Ana Gestora", "motivo": "valor fora da tabela"})
    assert r.status_code == 200, r.text
    return api["cliente"].post(rota, json=(chave or api["chave_org"]).assinar(r.json()["rascunho"]))


def test_recusa_pela_api(api, sessao):
    _, emitida = emitir(api, 900_000)
    oferta_id = emitida["oferta"]["id"]
    r = recusar(api, oferta_id)
    assert r.status_code == 201, r.text
    assert r.json()["recusado_por"] == "Ana Gestora"

    [o] = api["cliente"].get("/ofertas", headers=sessao).json()
    assert o["status"] == "recusada" and o["recusa"]["motivo"] == "valor fora da tabela"
    assert api["cliente"].get("/ofertas", params={"status": "aguardando_aprovacao"},
                              headers=sessao).json() == []

    r = api["cliente"].post(f"/ofertas/{oferta_id}/aprovacao/rascunho",
                            json={"aprovado_por": "Ana Gestora"})
    assert r.status_code == 400 and "recusada" in r.json()["detail"]
    r = aceitar(api, oferta_id)
    assert r.status_code == 400 and "recusada" in r.json()["detail"]


def test_recusa_exige_chave_da_organizacao(api):
    _, emitida = emitir(api, 900_000)
    r = recusar(api, emitida["oferta"]["id"], chave=api["chave_agente"])
    assert r.status_code == 400 and "não confere" in r.json()["detail"]


def test_recusa_em_rota_de_outra_oferta_e_recusada(api):
    _, a = emitir(api, 900_000)
    _, b = emitir(api, 900_000)
    r = recusar(api, a["oferta"]["id"], rota_id=b["oferta"]["id"])
    assert r.status_code == 400 and "outra oferta" in r.json()["detail"]


def test_oferta_dentro_do_mandato_nao_pode_ser_recusada(api):
    _, emitida = emitir(api)
    r = api["cliente"].post(f"/ofertas/{emitida['oferta']['id']}/recusa/rascunho",
                            json={"recusado_por": "Ana Gestora"})
    assert r.status_code == 400 and "enviada" in r.json()["detail"]


def test_recusa_de_oferta_inexistente_vira_404(api):
    r = api["cliente"].post("/ofertas/of_naoexiste/recusa/rascunho",
                            json={"recusado_por": "Ana Gestora"})
    assert r.status_code == 404
