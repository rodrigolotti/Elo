"""Histórico: cada frete com a linha do tempo (pedido, oferta, aprovação ou recusa, aceite)."""

import pytest

from conftest import CNPJ_EMBARC, CNPJ_TRANSP
from elo.modelos import Condicoes
from test_api import api, cliente, emitir, plataforma  # noqa: F401  (fixtures)
from test_api_painel import entrar

VALIDADE = "2026-11-18T18:00:00-03:00"
LOTACAO = {"modalidade": "lotacao", "distancia_km": 500, "eixos": 5}


def _emitir(cenario, valor, **extra):
    cond = Condicoes(valor_centavos=valor, coleta_ate="2026-11-20", prazo_entrega_dias_uteis=2,
                     **({"modalidade": "fracionada"} | extra))
    return cenario["plataforma"].emitir_oferta(cenario["pedido_id"], cenario["agente_id"],
                                               cenario["mandato_id"], cenario["chave_agente"],
                                               cond, VALIDADE)


def _etapas(frete):
    return [(e["etapa"], e["por"], e["detalhe"]) for e in frete["linha_do_tempo"]]


@pytest.fixture
def fretes(cenario):
    p, relogio = cenario["plataforma"], cenario["relogio"]
    dentro, _, token = _emitir(cenario, 485_000)
    relogio.avancar(minutes=5)
    p.aceitar_oferta(dentro["id"], CNPJ_EMBARC, "Maria Exemplo", "maria@exemplo.com", token)
    relogio.avancar(minutes=5)
    abaixo, _, token2 = _emitir(cenario, 300_000, **LOTACAO)
    relogio.avancar(minutes=5)
    p.aprovar_oferta(abaixo["id"], cenario["chave_org"], "Ana Gestora",
                     "Cliente estratégico; contrato anual.")
    relogio.avancar(minutes=5)
    p.aceitar_oferta(abaixo["id"], CNPJ_EMBARC, "Maria Exemplo", "maria@exemplo.com", token2)
    relogio.avancar(minutes=5)
    recusada, _, _ = _emitir(cenario, 900_000)
    p.recusar_oferta(recusada["id"], cenario["chave_org"], "Ana Gestora", "valor alto")
    relogio.avancar(minutes=5)
    aberta, _, _ = _emitir(cenario, 900_000)
    return {"p": p, "dentro": dentro, "abaixo": abaixo, "recusada": recusada, "aberta": aberta}


def _frete(f, oferta):
    return next(x for x in f["p"].listar_fretes(CNPJ_TRANSP) if x["oferta"]["id"] == oferta["id"])


def test_frete_dentro_do_mandato_e_aceito(fretes):
    frete = _frete(fretes, fretes["dentro"])
    assert frete["situacao"] == "fechado" and frete["verificacao"].startswith("/verificar/cp_")
    assert _etapas(frete) == [("pedido", None, None), ("oferta", "Agente de cotação", "dentro do mandato"),
                              ("aceite", "Maria Exemplo", None)]


def test_frete_aprovado_mostra_quem_aprovou_e_a_justificativa(fretes):
    frete = _frete(fretes, fretes["abaixo"])
    etapas = _etapas(frete)
    assert [e[0] for e in etapas] == ["pedido", "oferta", "aprovacao", "aceite"]
    assert etapas[1][2].startswith("abaixo do piso mínimo da ANTT")
    assert etapas[2] == ("aprovacao", "Ana Gestora", "Cliente estratégico; contrato anual.")
    tempos = [e["em"] for e in frete["linha_do_tempo"][1:]]
    assert tempos == sorted(tempos)


def test_frete_recusado_e_em_aberto(fretes):
    recusada = _frete(fretes, fretes["recusada"])
    assert recusada["situacao"] == "recusado" and recusada["verificacao"] is None
    assert _etapas(recusada)[-1] == ("recusa", "Ana Gestora", "valor alto")
    aberta = _frete(fretes, fretes["aberta"])
    assert aberta["situacao"] == "em-aberto" and [e[0] for e in _etapas(aberta)] == ["pedido", "oferta"]


def test_filtro_por_situacao_e_ordem(fretes):
    p = fretes["p"]
    assert [f["situacao"] for f in p.listar_fretes(CNPJ_TRANSP, "fechado")] == ["fechado"] * 2
    assert len(p.listar_fretes(CNPJ_TRANSP, "recusado")) == 1
    assert len(p.listar_fretes(CNPJ_TRANSP, "em-aberto")) == 1
    todos = p.listar_fretes(CNPJ_TRANSP)
    assert todos[0]["oferta"]["id"] == fretes["aberta"]["id"]   # mais recente primeiro


def test_api_fretes(api):
    sessao = entrar(api["cliente"], api["chave_org"])
    emitir(api)
    r = api["cliente"].get("/fretes", params={"situacao": "em-aberto"}, headers=sessao).json()
    assert len(r) == 1 and r[0]["linha_do_tempo"][0]["etapa"] == "pedido"
    assert api["cliente"].get("/fretes", params={"situacao": "qualquer"},
                              headers=sessao).status_code == 422
    assert api["cliente"].get("/fretes").status_code == 401
