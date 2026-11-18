"""Modalidade, distância e eixos na oferta (assinados pelo agente) e a violação
"abaixo do piso mínimo da ANTT", que só vale para carga lotação."""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import CNPJ_EMBARC, CNPJ_TRANSP
from elo.chaves import verificar_assinatura
from elo.mandato import AVISO_PISO, piso_da_oferta
from elo.modelos import Carga, Condicoes
from test_api import corpo_oferta, api, cliente, plataforma  # noqa: F401  (fixtures)

VALIDADE = "2026-11-18T18:00:00-03:00"
BRT = timezone(timedelta(hours=-3))


def condicoes(valor=300_000, **extra):
    return Condicoes(valor_centavos=valor, coleta_ate="2026-11-20",
                     prazo_entrega_dias_uteis=2, **extra)


LOTACAO = {"modalidade": "lotacao", "distancia_km": 500, "eixos": 5}  # piso R$ 4.071,76


def emitir(cenario, cond):
    p = cenario["plataforma"]
    return p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"], cenario["mandato_id"],
                           cenario["chave_agente"], cond, VALIDADE)


def test_lotacao_abaixo_do_piso_vai_para_aprovacao_com_aviso_forte(cenario):
    oferta, problemas, _ = emitir(cenario, condicoes(300_000, **LOTACAO))
    [aviso] = problemas
    assert aviso.startswith(f"{AVISO_PISO} (piso de R$ 4.071,76 para 500 km, 5 eixos, Carga geral")
    assert "tabela da ANTT vigente desde 30/09/2026" in aviso
    assert cenario["plataforma"].status_oferta[oferta["id"]] == "aguardando_aprovacao"


def test_lotacao_no_piso_ou_acima_nao_e_violacao(cenario):
    for valor in (407_176, 485_000):
        _, problemas, _ = emitir(cenario, condicoes(valor, **LOTACAO))
        assert problemas == []


@pytest.mark.parametrize("extra", [
    {"modalidade": "fracionada", "distancia_km": 500, "eixos": 5},  # piso não vale
    {"distancia_km": 500, "eixos": 5},                               # modalidade não informada
    {"modalidade": "lotacao", "eixos": 5},                           # sem distância
    {"modalidade": "lotacao", "distancia_km": 500},                  # sem eixos
    {},                                                              # oferta como antes
], ids=["fracionada", "sem-modalidade", "sem-distancia", "sem-eixos", "como-antes"])
def test_sem_lotacao_completa_nao_ha_regra_de_piso(cenario, extra):
    _, problemas, _ = emitir(cenario, condicoes(100_000, **extra))
    assert problemas == []


def test_celula_sem_coeficiente_na_norma_nao_tem_piso(cenario):
    carga = Carga(**{**cenario["plataforma"].pedidos[cenario["pedido_id"]]["carga"],
                     "tipo": "conteinerizada"})
    assert piso_da_oferta(carga, condicoes(1, modalidade="lotacao", distancia_km=500, eixos=2),
                          datetime(2026, 11, 17, tzinfo=BRT)) is None


def test_antes_da_primeira_tabela_nao_ha_piso(cenario):
    carga = Carga(**cenario["plataforma"].pedidos[cenario["pedido_id"]]["carga"])
    cond = condicoes(1, **LOTACAO)
    assert piso_da_oferta(carga, cond, datetime(2026, 9, 29, 12, tzinfo=BRT)) is None
    assert piso_da_oferta(carga, cond, datetime(2026, 9, 30, 0, tzinfo=BRT))["piso_centavos"] \
        == 407_176


def test_campos_vao_assinados_e_oferta_sem_eles_fica_como_antes(cenario):
    p = cenario["plataforma"]
    oferta, _, _ = emitir(cenario, condicoes(485_000, **LOTACAO))
    assert {k: oferta["condicoes"][k] for k in LOTACAO} == LOTACAO
    assert verificar_assinatura(oferta, p.agentes[cenario["agente_id"]]["chave_publica"])
    adulterada = {**oferta, "condicoes": {**oferta["condicoes"], "distancia_km": 50}}
    assert not verificar_assinatura(adulterada, p.agentes[cenario["agente_id"]]["chave_publica"])
    antiga, _, _ = emitir(cenario, condicoes(485_000))
    assert set(antiga["condicoes"]) == {"valor_centavos", "coleta_ate",
                                        "prazo_entrega_dias_uteis", "inclui"}


def test_compromisso_abaixo_do_piso_aprovado_continua_valido(cenario):
    p = cenario["plataforma"]
    oferta, _, token = emitir(cenario, condicoes(300_000, **LOTACAO))
    p.aprovar_oferta(oferta["id"], cenario["chave_org"], "Ana Gestora")
    cp = p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)
    p._reconstruir()
    relatorio = p.verificar_compromisso(cp["id"])
    assert relatorio.valido
    autorizada = next(v for v in relatorio.verificacoes if v.nome == "Oferta autorizada")
    assert AVISO_PISO in autorizada.detalhe and "aprovada por Ana Gestora" in autorizada.detalhe


def test_sem_aprovacao_o_compromisso_abaixo_do_piso_nao_seria_autorizado(cenario):
    p = cenario["plataforma"]
    oferta, _, _ = emitir(cenario, condicoes(300_000, **LOTACAO))
    assert p.status_oferta[oferta["id"]] == "aguardando_aprovacao"  # não pode ser aceita direto


# ---------- pela API ----------

@pytest.mark.parametrize("extra", [{"eixos": 8}, {"distancia_km": 0}, {"distancia_km": 10.5},
                                   {"modalidade": "carreta"}],
                         ids=["eixos-8", "distancia-0", "distancia-float", "modalidade"])
def test_api_recusa_campos_invalidos(api, extra):
    corpo = corpo_oferta(api)
    corpo["condicoes"] = {**corpo["condicoes"], **extra}
    assert api["cliente"].post("/ofertas/rascunho", json=corpo).status_code == 422


def test_api_preve_aprovacao_para_oferta_abaixo_do_piso(api):
    corpo = corpo_oferta(api, valor=300_000)
    corpo["condicoes"] = {**corpo["condicoes"], **LOTACAO}
    r = api["cliente"].post("/ofertas/rascunho", json=corpo).json()
    assert r["status_previsto"] == "aguardando_aprovacao"
    assert r["violacoes"][0].startswith(AVISO_PISO)
    assert r["rascunho"]["condicoes"]["modalidade"] == "lotacao"
