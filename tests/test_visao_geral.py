"""Visão geral: números do mês (horário de Brasília) para a tela inicial do painel."""

from datetime import datetime, timezone

from conftest import CNPJ_EMBARC, CNPJ_TRANSP, condicoes
from elo.modelos import Condicoes
from test_api import api, cliente, corpo_oferta, emitir, aceitar, plataforma  # noqa: F401
from test_api_painel import entrar

VALIDADE_LONGA = "2026-12-05T18:00:00-03:00"
LOTACAO = {"modalidade": "lotacao", "distancia_km": 500, "eixos": 5}  # piso R$ 4.071,76


def _emitir(cenario, valor, **extra):
    p = cenario["plataforma"]
    cond = Condicoes(valor_centavos=valor, coleta_ate="2026-11-20", prazo_entrega_dias_uteis=2,
                     **({"modalidade": "fracionada"} | extra))
    return p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"], cenario["mandato_id"],
                           cenario["chave_agente"], cond, VALIDADE_LONGA)


def _fechar(cenario, valor):
    oferta, _, token = _emitir(cenario, valor)
    return cenario["plataforma"].aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria",
                                                "maria@exemplo.com", token)


def test_numeros_do_mes(cenario):
    p = cenario["plataforma"]
    _fechar(cenario, 485_000)
    _fechar(cenario, 520_000)
    _emitir(cenario, 900_000)                    # acima do limite: aguardando
    _emitir(cenario, 300_000, **LOTACAO)         # abaixo do piso: aguardando e barrada
    v = p.visao_geral(CNPJ_TRANSP)
    assert v["mes"] == "2026-11"
    assert v["fechados"] == {"quantidade": 2, "total_centavos": 1_005_000}
    assert v["aguardando"] == {"quantidade": 2, "total_centavos": 1_200_000}
    assert v["barradas_piso"] == {"quantidade": 1, "abaixo_do_minimo_centavos": 107_176}
    assert v["embarcadores"] == [{"cnpj": CNPJ_EMBARC, "nome": None, "fretes": 2,
                                  "total_centavos": 1_005_000}]


def test_barrada_continua_contando_depois_de_decidida(cenario):
    p = cenario["plataforma"]
    oferta, _, _ = _emitir(cenario, 300_000, **LOTACAO)
    p.recusar_oferta(oferta["id"], cenario["chave_org"], "Ana")
    v = p.visao_geral(CNPJ_TRANSP)
    assert v["barradas_piso"]["quantidade"] == 1 and v["aguardando"]["quantidade"] == 0


def test_virada_do_mes_pelo_horario_de_brasilia(cenario):
    p, relogio = cenario["plataforma"], cenario["relogio"]
    relogio.agora = datetime(2026, 12, 1, 1, 0, tzinfo=timezone.utc)   # 30/11 às 22:00 em Brasília
    _fechar(cenario, 485_000)
    assert p.visao_geral(CNPJ_TRANSP)["fechados"]["quantidade"] == 1
    assert p.visao_geral(CNPJ_TRANSP)["mes"] == "2026-11"
    relogio.agora = datetime(2026, 12, 1, 4, 0, tzinfo=timezone.utc)   # 01/12 às 01:00 em Brasília
    v = p.visao_geral(CNPJ_TRANSP)
    assert v["mes"] == "2026-12" and v["fechados"]["quantidade"] == 0


def test_api_visao_geral_traz_as_ofertas_aguardando_com_o_frete(api):
    sessao = entrar(api["cliente"], api["chave_org"])
    _, emitida = emitir(api, 900_000)
    v = api["cliente"].get("/visao-geral", headers=sessao).json()
    [item] = v["aguardando"]["itens"]
    assert item["oferta"]["id"] == emitida["oferta"]["id"]
    assert "frete" in item and "embarcador" in item
    assert api["cliente"].get("/visao-geral").status_code == 401
