"""Chave de recuperação, rotação da chave principal e histórico de chaves (15ª checagem)."""

import pytest

from conftest import CNPJ_EMBARC, CNPJ_TRANSP, condicoes
from elo.canonico import hash_sha256
from elo.chaves import ParDeChaves
from elo.modelos import LimitesMandato
from elo.servico import CHECAGENS, ErroElo
from test_api import (  # noqa: F401  (fixtures)
    LIMITES, aceitar, api, assinar_e_enviar, cliente, emitir, plataforma,
)
from test_api_painel import desafio, entrar

VALIDADE = "2026-11-18T18:00:00-03:00"
HISTORICO = "Histórico de chaves da organização íntegro"


def checagem(relatorio, nome):
    return next(v for v in relatorio.verificacoes if v.nome == nome)


def compromisso(cenario, valor=485_000, chave_org=None):
    p = cenario["plataforma"]
    oferta, problemas, token = p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"],
                                               cenario["mandato_id"], cenario["chave_agente"],
                                               condicoes(valor), VALIDADE)
    if problemas:
        p.aprovar_oferta(oferta["id"], chave_org or cenario["chave_org"], "Ana Gestora")
    return p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)


@pytest.fixture
def com_recuperacao(cenario):
    rec = ParDeChaves.gerar("transp#recuperacao1")
    cenario["plataforma"].definir_recuperacao(CNPJ_TRANSP, cenario["chave_org"], rec)
    cenario["recuperacao"] = rec
    return cenario


def rotacionar(cenario):
    nova, nova_rec = ParDeChaves.gerar("transp#k2"), ParDeChaves.gerar("transp#recuperacao2")
    cenario["plataforma"].rotacionar_chave(CNPJ_TRANSP, cenario["recuperacao"], nova, nova_rec)
    antiga = cenario["chave_org"]
    cenario.update(chave_org=nova, recuperacao=nova_rec, chave_antiga=antiga)
    return cenario


# ---------- definição da chave de recuperação ----------

def test_sem_chave_de_recuperacao_nao_ha_rotacao(cenario):
    with pytest.raises(ErroElo, match="não tem chave de recuperação"):
        cenario["plataforma"].rotacionar_chave(CNPJ_TRANSP, ParDeChaves.gerar("x"),
                                               ParDeChaves.gerar("y"), ParDeChaves.gerar("z"))


def test_recuperacao_e_definida_pela_chave_principal(cenario):
    p = cenario["plataforma"]
    rec = ParDeChaves.gerar("rec")
    for intruso in (ParDeChaves.gerar("intrusa"), cenario["chave_agente"]):
        with pytest.raises(ErroElo):
            p.definir_recuperacao(CNPJ_TRANSP, intruso, rec)
    with pytest.raises(ErroElo, match="diferente da chave principal"):
        p.definir_recuperacao(CNPJ_TRANSP, cenario["chave_org"], cenario["chave_org"])
    p.definir_recuperacao(CNPJ_TRANSP, cenario["chave_org"], rec)
    assert p.recuperacao_org[CNPJ_TRANSP]["chave_publica"] == rec.publica_b64
    assert [e.valido for e in p.eventos_chave[CNPJ_TRANSP]] == [True]


def test_trocar_a_recuperacao_invalida_o_arquivo_antigo(com_recuperacao):
    p, antiga = com_recuperacao["plataforma"], com_recuperacao["recuperacao"]
    p.definir_recuperacao(CNPJ_TRANSP, com_recuperacao["chave_org"], ParDeChaves.gerar("rec2"))
    with pytest.raises(ErroElo, match="não confere"):
        p.rotacionar_chave(CNPJ_TRANSP, antiga, ParDeChaves.gerar("k2"), ParDeChaves.gerar("r3"))


# ---------- rotação ----------

def test_rotacao_troca_a_chave_principal_e_a_de_recuperacao(com_recuperacao):
    p = com_recuperacao["plataforma"]
    c = rotacionar(com_recuperacao)
    assert p._chave_atual(CNPJ_TRANSP).chave_publica == c["chave_org"].publica_b64
    assert p.recuperacao_org[CNPJ_TRANSP]["chave_publica"] == c["recuperacao"].publica_b64
    limites = LimitesMandato(**LIMITES)
    with pytest.raises(ErroElo, match="não é a chave desta organização"):
        p.emitir_mandato(CNPJ_TRANSP, c["chave_antiga"], c["agente_id"], limites)
    p.emitir_mandato(CNPJ_TRANSP, c["chave_org"], c["agente_id"], limites)


def test_arquivo_de_recuperacao_usado_nao_serve_de_novo(com_recuperacao):
    usada = com_recuperacao["recuperacao"]
    c = rotacionar(com_recuperacao)
    with pytest.raises(ErroElo, match="não confere"):
        c["plataforma"].rotacionar_chave(CNPJ_TRANSP, usada, ParDeChaves.gerar("k3"),
                                         ParDeChaves.gerar("r3"))
    rotacionar(c)  # a recuperação nova funciona


def test_rotacao_exige_a_chave_de_recuperacao(com_recuperacao):
    p = com_recuperacao["plataforma"]
    for errada in (com_recuperacao["chave_org"], com_recuperacao["chave_agente"]):
        with pytest.raises(ErroElo, match="não confere"):
            p.rotacionar_chave(CNPJ_TRANSP, errada, ParDeChaves.gerar("k2"),
                               ParDeChaves.gerar("r2"))


def test_chaves_novas_precisam_ser_novas(com_recuperacao):
    p, c = com_recuperacao["plataforma"], com_recuperacao
    nova = ParDeChaves.gerar("k2")
    for chave, rec in ((c["chave_org"], ParDeChaves.gerar("r2")),
                       (nova, c["recuperacao"]), (nova, nova)):
        with pytest.raises(ErroElo, match="novas e diferentes"):
            p.rotacionar_chave(CNPJ_TRANSP, c["recuperacao"], chave, rec)


def test_rascunho_da_chave_antiga_morre_na_rotacao(com_recuperacao):
    p = com_recuperacao["plataforma"]
    pendente = p.preparar_mandato(CNPJ_TRANSP, com_recuperacao["agente_id"],
                                  LimitesMandato(**LIMITES))
    c = rotacionar(com_recuperacao)
    with pytest.raises(ErroElo, match="rascunho desconhecido"):
        p.registrar_assinado(c["chave_antiga"].assinar(pendente), "mandato")


def test_dois_rascunhos_de_rotacao_so_um_vale(com_recuperacao):
    p, rec = com_recuperacao["plataforma"], com_recuperacao["recuperacao"]
    a = p.preparar_rotacao(CNPJ_TRANSP, "k2", ParDeChaves.gerar("k2").publica_b64,
                           "r2", ParDeChaves.gerar("r2").publica_b64)
    b = p.preparar_rotacao(CNPJ_TRANSP, "k3", ParDeChaves.gerar("k3").publica_b64,
                           "r3", ParDeChaves.gerar("r3").publica_b64)
    p.registrar_assinado(rec.assinar(a), "rotacao_chave")
    with pytest.raises(ErroElo):
        p.registrar_assinado(rec.assinar(b), "rotacao_chave")
    assert len(p.chaves_org[CNPJ_TRANSP]) == 2


# ---------- verificação depois da rotação ----------

def test_compromisso_anterior_continua_valido_depois_da_rotacao(com_recuperacao):
    p = com_recuperacao["plataforma"]
    antes = compromisso(com_recuperacao, 900_000)  # aprovado com a chave original
    c = rotacionar(com_recuperacao)
    depois = compromisso(c, 900_000)              # aprovado com a chave nova
    p._reconstruir()                              # como depois de reiniciar a API
    for cp in (antes, depois):
        relatorio = p.verificar_compromisso(cp["id"])
        assert relatorio.valido, [v for v in relatorio.verificacoes if not v.ok]
        assert len(relatorio.verificacoes) == len(CHECAGENS) == 15
        assert "1 rotação" in checagem(relatorio, HISTORICO).detalhe


def test_sem_rotacao_o_historico_diz_chave_original(cenario):
    relatorio = cenario["plataforma"].verificar_compromisso(compromisso(cenario)["id"])
    assert checagem(relatorio, HISTORICO).ok
    assert checagem(relatorio, HISTORICO).detalhe == "chave original, sem rotação"


def test_aprovacao_com_chave_antiga_gravada_depois_da_rotacao_falha(com_recuperacao):
    p = com_recuperacao["plataforma"]
    oferta, _, token = p.emitir_oferta(com_recuperacao["pedido_id"],
                                       com_recuperacao["agente_id"],
                                       com_recuperacao["mandato_id"],
                                       com_recuperacao["chave_agente"],
                                       condicoes(900_000), VALIDADE)
    c = rotacionar(com_recuperacao)
    # gravada por fora das regras, com a chave que já não vale
    p._incluir("oferta_aprovada", c["chave_antiga"].assinar({
        "tipo": "aprovacao", "oferta_hash": hash_sha256(oferta), "aprovado_por": "Alguém",
        "em": p._agora_iso()}), oferta["id"])
    cp = p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)
    relatorio = p.verificar_compromisso(cp["id"])
    assert not checagem(relatorio, "Oferta autorizada").ok
    assert checagem(relatorio, HISTORICO).ok  # o histórico em si está certo


# ---------- eventos de chave forjados ----------

def forjar_rotacao(p, assinante):
    atual = p._chave_atual(CNPJ_TRANSP)
    rec = p.recuperacao_org[CNPJ_TRANSP]
    falsa = ParDeChaves.gerar("transp#falsa")
    p._incluir("chave_rotacionada", assinante.assinar({
        "tipo": "rotacao_chave", "cnpj": CNPJ_TRANSP,
        "chave_anterior": {"chave_id": atual.chave_id, "chave_publica": atual.chave_publica},
        "chave_nova": {"chave_id": falsa.chave_id, "chave_publica": falsa.publica_b64},
        "recuperacao_usada": {"chave_id": rec["chave_id"],
                              "chave_publica": rec["chave_publica"]},
        "recuperacao_nova": {"chave_id": "r", "chave_publica": ParDeChaves.gerar("r").publica_b64},
        "em": p._agora_iso()}), CNPJ_TRANSP)
    return falsa


def test_rotacao_forjada_aparece_como_falha_e_nao_troca_a_chave(com_recuperacao):
    p = com_recuperacao["plataforma"]
    cp = compromisso(com_recuperacao)
    original = p._chave_atual(CNPJ_TRANSP).chave_publica
    falsa = forjar_rotacao(p, ParDeChaves.gerar("quem-forjou"))
    assert p._chave_atual(CNPJ_TRANSP).chave_publica == original
    relatorio = p.verificar_compromisso(cp["id"])
    hist = checagem(relatorio, HISTORICO)
    assert not relatorio.valido and not hist.ok
    assert "rotação de chave: assinatura não confere com a chave de recuperação vigente" \
        in hist.detalhe
    assert hist.detalhe.startswith("evento ")
    # e quem forjou não consegue usar a chave falsa
    with pytest.raises(ErroElo):
        p.emitir_mandato(CNPJ_TRANSP, falsa, com_recuperacao["agente_id"],
                         LimitesMandato(**LIMITES))


def test_mandato_assinado_pela_chave_forjada_falha(com_recuperacao):
    p = com_recuperacao["plataforma"]
    falsa = forjar_rotacao(p, ParDeChaves.gerar("quem-forjou"))
    mandato = falsa.assinar({**p.mandatos[com_recuperacao["mandato_id"]], "id": "md_falso",
                             "limites": {**LIMITES, "valor_max_centavos": 99_000_000}})
    p._incluir("mandato_emitido", mandato, "md_falso")
    oferta, problemas, token = p.emitir_oferta(com_recuperacao["pedido_id"],
                                               com_recuperacao["agente_id"], "md_falso",
                                               com_recuperacao["chave_agente"],
                                               condicoes(900_000), VALIDADE)
    assert problemas == []  # o servidor confia no estado, mas a verificação não
    cp = p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)
    relatorio = p.verificar_compromisso(cp["id"])
    assert not checagem(relatorio, "Mandato assinado pela organização para este agente").ok
    assert not checagem(relatorio, HISTORICO).ok


def test_recuperacao_forjada_aparece_como_falha(cenario):
    p = cenario["plataforma"]
    cp = compromisso(cenario)
    p._incluir("chave_recuperacao_definida", ParDeChaves.gerar("intrusa").assinar({
        "tipo": "recuperacao_chave", "cnpj": CNPJ_TRANSP,
        "chave_principal_id": cenario["chave_org"].chave_id, "chave_id": "rec",
        "chave_publica": ParDeChaves.gerar("rec").publica_b64,
        "definida_em": p._agora_iso()}), CNPJ_TRANSP)
    assert CNPJ_TRANSP not in p.recuperacao_org
    hist = checagem(p.verificar_compromisso(cp["id"]), HISTORICO)
    assert not hist.ok and "assinatura não confere com a chave principal vigente" in hist.detalhe


def test_evento_de_chave_mal_formado_aparece_como_falha(com_recuperacao):
    p = com_recuperacao["plataforma"]
    cp = compromisso(com_recuperacao)
    p._incluir("chave_rotacionada", {"cnpj": CNPJ_TRANSP, "tipo": "rotacao_chave"}, CNPJ_TRANSP)
    hist = checagem(p.verificar_compromisso(cp["id"]), HISTORICO)
    assert not hist.ok and "mal formada" in hist.detalhe


# ---------- pela API ----------

def test_recuperacao_e_rotacao_pela_api(api):
    c, chave_org = api["cliente"], api["chave_org"]
    rec = ParDeChaves.gerar("transp#rec1")
    _, r = assinar_e_enviar(c, "/organizacoes/recuperacao/rascunho", {
        "cnpj": CNPJ_TRANSP, "chave_id": rec.chave_id, "chave_publica": rec.publica_b64,
    }, chave_org, "/organizacoes/recuperacao")
    assert r.status_code == 201, r.text
    sessao = entrar(c, chave_org)
    assert c.get("/organizacao", headers=sessao).json()["recuperacao"]["chave_id"] == rec.chave_id

    # rotação sem sessão nenhuma: é o caminho de quem perdeu a chave
    nova, nova_rec = ParDeChaves.gerar("transp#k2"), ParDeChaves.gerar("transp#rec2")
    _, r = assinar_e_enviar(c, "/organizacoes/rotacao/rascunho", {
        "cnpj": CNPJ_TRANSP, "chave_id": nova.chave_id, "chave_publica": nova.publica_b64,
        "recuperacao_chave_id": nova_rec.chave_id,
        "recuperacao_chave_publica": nova_rec.publica_b64,
    }, rec, "/organizacoes/rotacao")
    assert r.status_code == 201, r.text

    assert c.get("/agentes", headers=sessao).status_code == 401  # sessão antiga encerrada
    assert c.post("/sessoes", json=chave_org.assinar(desafio(c))).status_code == 401
    resumo = c.get("/organizacao", headers=entrar(c, nova)).json()
    assert resumo["chave_atual"]["chave_id"] == "transp#k2"
    assert resumo["recuperacao"]["chave_id"] == "transp#rec2"
    assert [h["valido"] for h in resumo["historico_chaves"]] == [True, True]


def test_rotacao_pela_api_assinada_pela_chave_principal_e_recusada(api):
    c = api["cliente"]
    rec = ParDeChaves.gerar("transp#rec1")
    assinar_e_enviar(c, "/organizacoes/recuperacao/rascunho", {
        "cnpj": CNPJ_TRANSP, "chave_id": rec.chave_id, "chave_publica": rec.publica_b64,
    }, api["chave_org"], "/organizacoes/recuperacao")
    nova, nova_rec = ParDeChaves.gerar("k2"), ParDeChaves.gerar("r2")
    _, r = assinar_e_enviar(c, "/organizacoes/rotacao/rascunho", {
        "cnpj": CNPJ_TRANSP, "chave_id": "k2", "chave_publica": nova.publica_b64,
        "recuperacao_chave_id": "r2", "recuperacao_chave_publica": nova_rec.publica_b64,
    }, api["chave_org"], "/organizacoes/rotacao")
    assert r.status_code == 400 and "não confere" in r.json()["detail"]


def test_pagina_publica_mostra_a_15a_checagem(api):
    _, emitida = emitir(api)
    cp_id = aceitar(api, emitida["oferta"]["id"]).json()["id"]
    texto = api["cliente"].get(f"/verificar/{cp_id}").text
    assert HISTORICO in texto and "15 de 15 checagens OK" in texto


def test_resumo_traz_as_datas_de_cada_acesso(com_recuperacao):
    p, relogio = com_recuperacao["plataforma"], com_recuperacao["relogio"]
    resumo = p.resumo_organizacao(CNPJ_TRANSP)
    assert resumo["chave_atual"]["desde"] == "2026-11-17T09:00:00-03:00"
    assert resumo["recuperacao"]["definida_em"] == "2026-11-17T09:00:00-03:00"
    relogio.avancar(hours=5)
    rotacionar(com_recuperacao)
    resumo = p.resumo_organizacao(CNPJ_TRANSP)
    assert resumo["chave_atual"]["desde"] == "2026-11-17T14:00:00-03:00"
    assert resumo["recuperacao"]["definida_em"] == "2026-11-17T14:00:00-03:00"
    assert [h["em"] for h in resumo["historico_chaves"]] == [
        "2026-11-17T09:00:00-03:00", "2026-11-17T14:00:00-03:00"]
