"""Tipo de carga como lista da ANTT no pedido e no mandato, com dados antigos em texto livre."""

import pytest

from conftest import CNPJ_EMBARC, CNPJ_TRANSP, condicoes
from elo.antt import TIPOS_CARGA, nome_tipo_carga, normalizar_tipo_carga
from elo.modelos import Carga, LimitesMandato
from elo.servico import ErroElo
from test_api import CARGA, LIMITES, api, cliente, plataforma  # noqa: F401  (fixtures)

VALIDADE = "2026-11-18T18:00:00-03:00"


@pytest.mark.parametrize("texto, categoria", [
    ("carga geral", "carga-geral"), ("Carga Geral", "carga-geral"), ("carga-geral", "carga-geral"),
    ("Granel Sólido", "granel-solido"), ("FRIGORIFICADA OU AQUECIDA", "frigorificada"),
    ("Perigosa (carga geral)", "perigosa-carga-geral"),
    ("Carga Granel Pressurizada", "granel-pressurizada"), ("mudança", None), ("", None)])
def test_normalizacao(texto, categoria):
    assert normalizar_tipo_carga(texto) == categoria


def test_nome_para_exibir():
    assert nome_tipo_carga("carga-geral") == "Carga geral"
    assert nome_tipo_carga("mudança residencial") == "mudança residencial"


def _carga(tipo):
    return Carga(**{**CARGA, "tipo": tipo})


def test_pedido_guarda_a_categoria_e_recusa_texto_fora_da_lista(cenario):
    p = cenario["plataforma"]
    assert p.registrar_pedido(CNPJ_EMBARC, "email", _carga("Granel Sólido"))["carga"]["tipo"] \
        == "granel-solido"
    with pytest.raises(ErroElo, match="fora da lista da ANTT: 'mudança'.*carga-geral"):
        p.registrar_pedido(CNPJ_EMBARC, "email", _carga("mudança"))


def test_mandato_guarda_categorias_sem_repetir_e_recusa_texto_fora_da_lista(cenario):
    p = cenario["plataforma"]
    limites = LimitesMandato(**{**LIMITES, "tipos_carga": ["Carga Geral", "carga geral",
                                                           "Frigorificada ou aquecida"]})
    m = p.emitir_mandato(CNPJ_TRANSP, cenario["chave_org"], cenario["agente_id"], limites)
    assert m["limites"]["tipos_carga"] == ["carga-geral", "frigorificada"]
    with pytest.raises(ErroElo, match="fora da lista da ANTT"):
        p.emitir_mandato(CNPJ_TRANSP, cenario["chave_org"], cenario["agente_id"],
                         LimitesMandato(**{**LIMITES, "tipos_carga": ["qualquer coisa"]}))


def test_categoria_fora_do_mandato_e_violacao(cenario):
    p = cenario["plataforma"]
    pedido = p.registrar_pedido(CNPJ_EMBARC, "email", _carga("frigorificada"))
    _, problemas, _ = p.emitir_oferta(pedido["id"], cenario["agente_id"], cenario["mandato_id"],
                                      cenario["chave_agente"], condicoes(), VALIDADE)
    assert "tipo de carga 'Frigorificada ou aquecida' não autorizado" in problemas


def _antigos(p, cenario, tipo_mandato, tipo_pedido):
    """Grava mandato e pedido como eram antes da lista (texto livre), por fora das regras."""
    mandato = cenario["chave_org"].assinar({
        **p.mandatos[cenario["mandato_id"]], "id": "md_antigo",
        "limites": {**LIMITES, "tipos_carga": [tipo_mandato]}})
    p._incluir("mandato_emitido", mandato, "md_antigo")
    pedido = {**p.pedidos[cenario["pedido_id"]], "id": "pc_antigo",
              "carga": {**CARGA, "tipo": tipo_pedido}}
    p._incluir("pedido_recebido", pedido, "pc_antigo")


@pytest.mark.parametrize("tipo_mandato, tipo_pedido", [
    ("Carga Geral", "carga geral"),        # antigos que viram a mesma categoria
    ("mudança", "mudança"),                # antigo fora da lista: compara o texto
])
def test_compromisso_com_dados_antigos_continua_valido(cenario, tipo_mandato, tipo_pedido):
    p = cenario["plataforma"]
    _antigos(p, cenario, tipo_mandato, tipo_pedido)
    oferta, problemas, token = p.emitir_oferta("pc_antigo", cenario["agente_id"], "md_antigo",
                                               cenario["chave_agente"], condicoes(), VALIDADE)
    assert problemas == []
    cp = p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)
    p._reconstruir()
    relatorio = p.verificar_compromisso(cp["id"])
    assert relatorio.valido, [v for v in relatorio.verificacoes if not v.ok]


def test_api_recusa_tipo_fora_da_lista(api):
    r = api["cliente"].post("/pedidos", json={"cnpj_solicitante": CNPJ_EMBARC, "canal": "email",
                                              "carga": {**CARGA, "tipo": "mudança"}})
    assert r.status_code == 400 and "fora da lista da ANTT" in r.json()["detail"]
    r = api["cliente"].post("/mandatos/rascunho", json={
        "cnpj": CNPJ_TRANSP, "agente_id": api["agente_id"],
        "limites": {**LIMITES, "tipos_carga": ["mudança"]}})
    assert r.status_code == 400 and "fora da lista da ANTT" in r.json()["detail"]


def test_lista_tem_as_12_categorias_da_tabela_a():
    assert len(TIPOS_CARGA) == 12 and list(TIPOS_CARGA)[0] == "carga-geral"
