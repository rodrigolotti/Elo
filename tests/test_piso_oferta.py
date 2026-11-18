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
    {"modalidade": "fracionada"},                                    # nem exige distância
], ids=["fracionada", "fracionada-sem-dados"])
def test_fora_da_lotacao_nao_ha_regra_de_piso(cenario, extra):
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
    fracionada, _, _ = emitir(cenario, condicoes(485_000, modalidade="fracionada"))
    assert set(fracionada["condicoes"]) == {"valor_centavos", "coleta_ate",
                                            "prazo_entrega_dias_uteis", "inclui", "modalidade"}


def test_compromisso_abaixo_do_piso_aprovado_continua_valido(cenario):
    p = cenario["plataforma"]
    oferta, _, token = emitir(cenario, condicoes(300_000, **LOTACAO))
    p.aprovar_oferta(oferta["id"], cenario["chave_org"], "Ana Gestora", JUSTIFICATIVA)
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


# ---------- o que o painel recebe: distância, valor por km e piso ----------

def _pendente(api, sessao, valor, **condicoes_extra):
    from test_api import CARGA
    c = api["cliente"]
    r = c.post("/pedidos", json={"cnpj_solicitante": CNPJ_EMBARC, "canal": "email",
                                 "carga": {**CARGA, "origem_cep": "01001-000",
                                           "destino_cep": "80010-000"}})
    corpo = {**corpo_oferta(api, valor=valor), "pedido_id": r.json()["id"]}
    corpo["condicoes"] = {**corpo["condicoes"], **condicoes_extra}
    rascunho = c.post("/ofertas/rascunho", json=corpo).json()["rascunho"]
    oferta = c.post("/ofertas", json=api["chave_agente"].assinar(rascunho)).json()["oferta"]
    return next(o for o in c.get("/ofertas", headers=sessao).json()
                if o["oferta"]["id"] == oferta["id"])["frete"]


@pytest.fixture
def sessao(api):
    from test_api_painel import entrar
    return entrar(api["cliente"], api["chave_org"])


def test_frete_com_distancia_do_agente(api, sessao):
    frete = _pendente(api, sessao, 900_000, **LOTACAO)
    assert frete["distancia"] == {"km": 500, "origem": "agente"}
    assert frete["valor_por_km_centavos"] == 1_800            # R$ 9.000 / 500 km = R$ 18,00
    assert frete["piso"]["piso_centavos"] == 407_176 and frete["piso"]["estimado"] is False
    assert frete["piso"]["folga_centavos"] == 900_000 - 407_176


def test_frete_com_distancia_estimada_pelo_cep(api, sessao):
    frete = _pendente(api, sessao, 900_000, modalidade="lotacao", eixos=5)
    assert frete["distancia"]["origem"] == "estimada" and frete["distancia"]["km"] == 427
    assert frete["distancia"]["detalhe"] == "São Paulo/SP → Curitiba/PR"
    assert frete["piso"]["estimado"] is True
    assert frete["piso"]["piso_centavos"] == (427 * 68284 + 50) // 100 + 65756


@pytest.mark.parametrize("extra, motivo", [
    ({"modalidade": "fracionada", "distancia_km": 500, "eixos": 5}, "carga fracionada"),
    ({"modalidade": "lotacao", "distancia_km": 500}, "não informou o número de eixos"),
])
def test_frete_explica_quando_nao_ha_piso(api, sessao, extra, motivo):
    frete = _pendente(api, sessao, 900_000, **extra)
    assert frete["piso"] is None and motivo in frete["sem_piso"]


# ---------- protocolo 0.2: lotação sem distância ou eixos vai para aprovação ----------

from elo.mandato import SEM_DISTANCIA, SEM_EIXOS  # noqa: E402
from elo.servico import ErroElo  # noqa: E402


@pytest.mark.parametrize("extra, motivos", [
    ({"modalidade": "lotacao", "eixos": 5}, [SEM_DISTANCIA]),
    ({"modalidade": "lotacao", "distancia_km": 500}, [SEM_EIXOS]),
    ({"modalidade": "lotacao"}, [SEM_DISTANCIA, SEM_EIXOS]),
], ids=["sem-distancia", "sem-eixos", "sem-nenhum"])
def test_lotacao_sem_distancia_ou_eixos_vai_para_aprovacao(cenario, extra, motivos):
    oferta, problemas, _ = emitir(cenario, condicoes(485_000, **extra))
    assert problemas == motivos
    assert oferta["versao_protocolo"] == "0.2"
    assert cenario["plataforma"].status_oferta[oferta["id"]] == "aguardando_aprovacao"
    assert SEM_DISTANCIA == "distância não informada: não foi possível conferir o piso da ANTT"


def test_oferta_0_1_sem_distancia_continua_valida(cenario, monkeypatch):
    """A regra nova não alcança ofertas emitidas antes dela (protocolo 0.1)."""
    import elo.servico
    p = cenario["plataforma"]
    monkeypatch.setattr(elo.servico, "VERSAO_PROTOCOLO", "0.1")
    oferta, problemas, token = emitir(cenario, condicoes(485_000, modalidade="lotacao", eixos=5))
    monkeypatch.undo()
    assert oferta["versao_protocolo"] == "0.1" and problemas == []
    cp = p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)
    p._reconstruir()
    assert p.verificar_compromisso(cp["id"]).valido


# ---------- aprovar abaixo do piso exige justificativa assinada ----------

JUSTIFICATIVA = "Cliente estratégico; diferença coberta pelo contrato anual de 2026."


def test_aprovar_abaixo_do_piso_exige_justificativa(cenario):
    p = cenario["plataforma"]
    oferta, _, _ = emitir(cenario, condicoes(300_000, **LOTACAO))
    for curta in ("", "   ", "porque si"):
        with pytest.raises(ErroElo, match="escreva a justificativa"):
            p.aprovar_oferta(oferta["id"], cenario["chave_org"], "Ana", curta)
    aprovacao = p.aprovar_oferta(oferta["id"], cenario["chave_org"], "Ana", JUSTIFICATIVA)
    assert aprovacao["justificativa"] == JUSTIFICATIVA
    chave = p._chave_atual(CNPJ_TRANSP).chave_publica
    assert verificar_assinatura(aprovacao, chave)
    assert not verificar_assinatura({**aprovacao, "justificativa": "outra"}, chave)


def test_justificativa_aparece_na_verificacao(cenario):
    p = cenario["plataforma"]
    oferta, _, token = emitir(cenario, condicoes(300_000, **LOTACAO))
    p.aprovar_oferta(oferta["id"], cenario["chave_org"], "Ana Gestora", JUSTIFICATIVA)
    cp = p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)
    autorizada = next(v for v in p.verificar_compromisso(cp["id"]).verificacoes
                      if v.nome == "Oferta autorizada")
    assert autorizada.ok and f"justificativa: {JUSTIFICATIVA}" in autorizada.detalhe


def test_aprovacao_sem_justificativa_gravada_por_fora_falha_na_verificacao(cenario):
    from elo.canonico import hash_sha256
    p = cenario["plataforma"]
    oferta, _, token = emitir(cenario, condicoes(300_000, **LOTACAO))
    p._incluir("oferta_aprovada", cenario["chave_org"].assinar({
        "tipo": "aprovacao", "oferta_hash": hash_sha256(oferta), "aprovado_por": "Alguém",
        "em": p._agora_iso()}), oferta["id"])
    cp = p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)
    autorizada = next(v for v in p.verificar_compromisso(cp["id"]).verificacoes
                      if v.nome == "Oferta autorizada")
    assert not autorizada.ok
    assert "abaixo do piso da ANTT e sem justificativa" in autorizada.detalhe


def test_aprovacao_comum_nao_exige_justificativa(cenario):
    p = cenario["plataforma"]
    oferta, problemas, _ = emitir(cenario, condicoes(900_000, **LOTACAO))  # só acima do limite
    assert problemas and not any(x.startswith(AVISO_PISO) for x in problemas)
    aprovacao = p.aprovar_oferta(oferta["id"], cenario["chave_org"], "Ana")
    assert "justificativa" not in aprovacao


def test_api_aprovacao_abaixo_do_piso(api):
    from test_api import assinar_e_enviar
    corpo = corpo_oferta(api, valor=300_000)
    corpo["condicoes"] = {**corpo["condicoes"], **LOTACAO}
    c = api["cliente"]
    rascunho = c.post("/ofertas/rascunho", json=corpo).json()["rascunho"]
    oferta_id = c.post("/ofertas", json=api["chave_agente"].assinar(rascunho)).json()["oferta"]["id"]
    r = c.post(f"/ofertas/{oferta_id}/aprovacao/rascunho", json={"aprovado_por": "Ana"})
    assert r.status_code == 400 and "escreva a justificativa" in r.json()["detail"]
    _, r = assinar_e_enviar(c, f"/ofertas/{oferta_id}/aprovacao/rascunho",
                            {"aprovado_por": "Ana", "justificativa": JUSTIFICATIVA},
                            api["chave_org"], f"/ofertas/{oferta_id}/aprovacao")
    assert r.status_code == 201 and r.json()["justificativa"] == JUSTIFICATIVA


def test_api_lotacao_sem_distancia_preve_aprovacao(api):
    corpo = corpo_oferta(api)
    corpo["condicoes"] = {**corpo["condicoes"], "modalidade": "lotacao", "eixos": 5}
    r = api["cliente"].post("/ofertas/rascunho", json=corpo).json()
    assert r["status_previsto"] == "aguardando_aprovacao" and r["violacoes"] == [SEM_DISTANCIA]


# ---------- protocolo 0.2: modalidade obrigatória e tipo fora da lista ----------

from elo.mandato import SEM_MODALIDADE, TIPO_FORA_DA_LISTA, violacoes  # noqa: E402
from elo.modelos import LimitesMandato  # noqa: E402
from test_api import LIMITES  # noqa: E402


@pytest.mark.parametrize("extra", [{}, {"distancia_km": 500, "eixos": 5}],
                         ids=["sem-nada", "sem-modalidade"])
def test_oferta_sem_modalidade_e_recusada(cenario, extra):
    with pytest.raises(ErroElo, match="precisa informar a modalidade: lotacao ou fracionada"):
        emitir(cenario, condicoes(485_000, **extra))


def test_api_recusa_oferta_sem_modalidade(api):
    corpo = corpo_oferta(api)
    corpo["condicoes"] = {k: v for k, v in corpo["condicoes"].items() if k != "modalidade"}
    r = api["cliente"].post("/ofertas/rascunho", json=corpo)
    assert r.status_code == 400 and "precisa informar a modalidade" in r.json()["detail"]


def test_regra_da_modalidade_depende_da_versao_do_protocolo(cenario):
    """Oferta 0.2 sem modalidade (só existiria num registro adulterado) não passa;
    a mesma oferta em 0.1 continua como foi emitida."""
    carga = Carga(**cenario["plataforma"].pedidos[cenario["pedido_id"]]["carga"])
    momento = datetime(2026, 11, 17, 9, tzinfo=BRT)
    sem_modalidade = condicoes(485_000)
    assert violacoes(LimitesMandato(**LIMITES), carga, sem_modalidade, momento, None, "0.2") \
        == [SEM_MODALIDADE]
    assert violacoes(LimitesMandato(**LIMITES), carga, sem_modalidade, momento, None, "0.1") == []
    assert violacoes(LimitesMandato(**LIMITES), carga, sem_modalidade, momento, None, None) == []


def test_lotacao_com_tipo_fora_da_lista_vai_para_aprovacao(cenario):
    """Pedido e mandato antigos em texto livre ("mudança"): o mandato aceita (mesmo
    texto), mas em lotação não há como conferir o piso."""
    from test_api import CARGA
    p = cenario["plataforma"]
    mandato = cenario["chave_org"].assinar({
        **p.mandatos[cenario["mandato_id"]], "id": "md_antigo",
        "limites": {**LIMITES, "tipos_carga": ["mudança"]}})
    p._incluir("mandato_emitido", mandato, "md_antigo")
    p._incluir("pedido_recebido", {**p.pedidos[cenario["pedido_id"]], "id": "pc_antigo",
                                   "carga": {**CARGA, "tipo": "mudança"}}, "pc_antigo")
    oferta, problemas, _ = p.emitir_oferta("pc_antigo", cenario["agente_id"], "md_antigo",
                                           cenario["chave_agente"],
                                           condicoes(485_000, **LOTACAO), VALIDADE)
    assert problemas == [TIPO_FORA_DA_LISTA]
    assert p.status_oferta[oferta["id"]] == "aguardando_aprovacao"
    _, problemas, _ = p.emitir_oferta("pc_antigo", cenario["agente_id"], "md_antigo",
                                      cenario["chave_agente"],
                                      condicoes(485_000, modalidade="fracionada"), VALIDADE)
    assert problemas == []  # fracionada: o piso não se aplica
