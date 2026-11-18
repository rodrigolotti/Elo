"""Testes da API HTTP com TestClient. As chaves ficam sempre do lado do teste (cliente)."""

import re

import pytest
from fastapi.testclient import TestClient

import demo_api
from conftest import CNPJ_EMBARC, CNPJ_TRANSP, cnpj_ficticio
from elo.api import criar_app
from elo.chaves import ParDeChaves
from elo.registro import Registro
from elo.servico import CHECAGENS, Plataforma

VALIDADE = "2026-11-18T18:00:00-03:00"
LIMITES = {
    "valor_max_centavos": 600_000, "ufs_origem": ["SP"], "ufs_destino": ["SP", "PR"],
    "tipos_carga": ["carga geral"], "valido_de": "2026-11-01T00:00:00-03:00",
    "valido_ate": "2026-12-31T23:59:59-03:00",
}
CARGA = {"origem_cep": "00000-000", "origem_uf": "SP", "destino_cep": "11111-111",
         "destino_uf": "PR", "peso_kg": 3200, "volume_litros": 14500,
         "tipo": "carga geral"}


def cond(valor=485_000):
    return {"valor_centavos": valor, "coleta_ate": "2026-11-20",
            "prazo_entrega_dias_uteis": 2, "inclui": ["pedagio"]}


@pytest.fixture
def plataforma(relogio):
    return Plataforma(Registro(), ParDeChaves.gerar("plataforma#teste"), relogio)


@pytest.fixture
def cliente(plataforma):
    return TestClient(criar_app(plataforma))


def assinar_e_enviar(cliente, rota_rascunho, corpo, chave, rota):
    r = cliente.post(rota_rascunho, json=corpo)
    assert r.status_code == 200, r.text
    return r.json(), cliente.post(rota, json=chave.assinar(r.json()["rascunho"]))


@pytest.fixture
def api(cliente):
    """Transportadora, agente, mandato e pedido criados pela API."""
    chave_org = ParDeChaves.gerar("transp#k1")
    chave_agente = ParDeChaves.gerar("agente#k1")
    _, r = assinar_e_enviar(cliente, "/organizacoes/rascunho", {
        "cnpj": CNPJ_TRANSP, "razao_social": "Transportadora Fictícia Ltda",
        "chave_id": chave_org.chave_id, "chave_publica": chave_org.publica_b64,
    }, chave_org, "/organizacoes")
    assert r.status_code == 201, r.text
    _, r = assinar_e_enviar(cliente, "/agentes/rascunho", {
        "cnpj": CNPJ_TRANSP, "nome": "Agente de cotação",
        "chave_id": chave_agente.chave_id, "chave_publica": chave_agente.publica_b64,
    }, chave_org, "/agentes")
    assert r.status_code == 201, r.text
    agente_id = r.json()["agente_id"]
    _, r = assinar_e_enviar(cliente, "/mandatos/rascunho", {
        "cnpj": CNPJ_TRANSP, "agente_id": agente_id, "limites": LIMITES,
    }, chave_org, "/mandatos")
    assert r.status_code == 201, r.text
    mandato_id = r.json()["id"]
    r = cliente.post("/pedidos", json={"cnpj_solicitante": CNPJ_EMBARC, "canal": "email",
                                       "carga": CARGA})
    assert r.status_code == 201, r.text
    return {"cliente": cliente, "chave_org": chave_org, "chave_agente": chave_agente,
            "agente_id": agente_id, "mandato_id": mandato_id, "pedido_id": r.json()["id"]}


def corpo_oferta(api, valor=485_000):
    return {"pedido_id": api["pedido_id"], "agente_id": api["agente_id"],
            "mandato_id": api["mandato_id"], "condicoes": cond(valor),
            "valida_ate": VALIDADE}


def emitir(api, valor=485_000):
    preparado, r = assinar_e_enviar(api["cliente"], "/ofertas/rascunho",
                                    corpo_oferta(api, valor), api["chave_agente"],
                                    "/ofertas")
    assert r.status_code == 201, r.text
    return preparado, r.json()


def aceitar(api, oferta_id, cnpj=CNPJ_EMBARC):
    return api["cliente"].post(f"/ofertas/{oferta_id}/aceite", json={
        "cnpj": cnpj, "nome": "Maria Exemplo", "email": "maria@exemplo.com"})


def status_na_pagina(texto):
    return re.findall(r'class="st (?:ok|falha)">(OK|FALHA)<', texto)


# ---------- fluxo ----------

def test_fluxo_completo_pela_api(api):
    preparado, emitida = emitir(api)
    assert preparado["violacoes"] == [] and preparado["status_previsto"] == "enviada"
    assert emitida["status"] == "enviada"
    r = aceitar(api, emitida["oferta"]["id"])
    assert r.status_code == 201, r.text
    cp_id = r.json()["id"]

    rel = api["cliente"].get(f"/compromissos/{cp_id}/verificacao").json()
    assert rel["valido"] is True
    assert [v["nome"] for v in rel["verificacoes"]] == list(CHECAGENS)
    assert all(v["ok"] for v in rel["verificacoes"])


def test_pagina_publica_mostra_14_checagens_ok(api):
    _, emitida = emitir(api)
    cp_id = aceitar(api, emitida["oferta"]["id"]).json()["id"]
    r = api["cliente"].get(f"/verificar/{cp_id}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert 'lang="pt-BR"' in r.text
    assert "COMPROMISSO VÁLIDO" in r.text
    assert status_na_pagina(r.text) == ["OK"] * 14
    for nome in CHECAGENS:
        assert nome in r.text
    assert "R$ 4.850,00" in r.text
    assert "maria@exemplo.com" not in r.text  # página pública não expõe o e-mail


def test_oferta_fora_do_mandato_exige_aprovacao_assinada(api):
    preparado, emitida = emitir(api, 900_000)
    assert any("acima do limite" in v for v in preparado["violacoes"])
    assert emitida["status"] == "aguardando_aprovacao"
    oferta_id = emitida["oferta"]["id"]

    r = aceitar(api, oferta_id)
    assert r.status_code == 400 and "aguardando_aprovacao" in r.json()["detail"]

    # chave errada: a assinatura não confere com a chave da organização
    _, r = assinar_e_enviar(api["cliente"], f"/ofertas/{oferta_id}/aprovacao/rascunho",
                            {"aprovado_por": "Intruso"}, ParDeChaves.gerar("intruso"),
                            f"/ofertas/{oferta_id}/aprovacao")
    assert r.status_code == 400

    _, r = assinar_e_enviar(api["cliente"], f"/ofertas/{oferta_id}/aprovacao/rascunho",
                            {"aprovado_por": "Gerente Comercial"}, api["chave_org"],
                            f"/ofertas/{oferta_id}/aprovacao")
    assert r.status_code == 201, r.text
    cp_id = aceitar(api, oferta_id).json()["id"]
    rel = api["cliente"].get(f"/compromissos/{cp_id}/verificacao").json()
    assert rel["valido"]
    autorizacao = next(v for v in rel["verificacoes"] if v["nome"] == "Oferta autorizada")
    assert "aprovada por Gerente Comercial" in autorizacao["detalhe"]


def test_aprovacao_em_rota_de_outra_oferta_e_recusada(api):
    _, a = emitir(api, 900_000)
    _, b = emitir(api, 900_000)
    rascunho = api["cliente"].post(f"/ofertas/{a['oferta']['id']}/aprovacao/rascunho",
                                   json={"aprovado_por": "Gerente"}).json()["rascunho"]
    r = api["cliente"].post(f"/ofertas/{b['oferta']['id']}/aprovacao",
                            json=api["chave_org"].assinar(rascunho))
    assert r.status_code == 400 and "outra oferta" in r.json()["detail"]


# ---------- assinatura do lado do cliente ----------

def test_api_nao_tem_campo_de_chave_privada(cliente):
    esquemas = cliente.get("/openapi.json").json()["components"]["schemas"]
    campos = {c.lower() for e in esquemas.values() for c in e.get("properties", {})}
    assert "chave_publica" in campos
    assert not any("privad" in c or "private" in c or "secret" in c for c in campos)


def test_agente_nao_assina_com_chave_alheia(api):
    _, r = assinar_e_enviar(api["cliente"], "/ofertas/rascunho", corpo_oferta(api),
                            ParDeChaves.gerar("falsa"), "/ofertas")
    assert r.status_code == 400
    assert "não confere" in r.json()["detail"]


def test_rascunho_alterado_antes_de_assinar_e_recusado(api):
    r = api["cliente"].post("/ofertas/rascunho", json=corpo_oferta(api))
    rascunho = r.json()["rascunho"]
    rascunho["condicoes"]["valor_centavos"] = 100  # agente tenta mudar o que foi preparado
    r = api["cliente"].post("/ofertas", json=api["chave_agente"].assinar(rascunho))
    assert r.status_code == 400 and "rascunho" in r.json()["detail"]


def test_objeto_assinado_nao_pode_ser_reenviado(api):
    r = api["cliente"].post("/ofertas/rascunho", json=corpo_oferta(api))
    assinado = api["chave_agente"].assinar(r.json()["rascunho"])
    assert api["cliente"].post("/ofertas", json=assinado).status_code == 201
    assert api["cliente"].post("/ofertas", json=assinado).status_code == 400


def test_rascunho_expira(api, relogio):
    r = api["cliente"].post("/ofertas/rascunho", json=corpo_oferta(api))
    relogio.avancar(minutes=11)
    r = api["cliente"].post("/ofertas", json=api["chave_agente"].assinar(r.json()["rascunho"]))
    assert r.status_code == 400 and "expirado" in r.json()["detail"]


def test_rascunho_de_outro_tipo_e_recusado(api):
    r = api["cliente"].post("/ofertas/rascunho", json=corpo_oferta(api))
    r = api["cliente"].post("/mandatos", json=api["chave_agente"].assinar(r.json()["rascunho"]))
    assert r.status_code == 400


def test_assinatura_mal_formada_e_recusada(api):
    r = api["cliente"].post("/ofertas/rascunho", json=corpo_oferta(api))
    r = api["cliente"].post("/ofertas", json={**r.json()["rascunho"], "assinatura": "x"})
    assert r.status_code == 400


# ---------- erros e validação ----------

def test_cnpj_invalido_e_organizacao_duplicada(api):
    chave = ParDeChaves.gerar("k")
    corpo = {"cnpj": "11.111.111/1111-11", "razao_social": "X", "chave_id": "k",
             "chave_publica": chave.publica_b64}
    assert api["cliente"].post("/organizacoes/rascunho", json=corpo).status_code == 400
    r = api["cliente"].post("/organizacoes/rascunho", json={**corpo, "cnpj": CNPJ_TRANSP})
    assert r.status_code == 400 and "já cadastrada" in r.json()["detail"]


def test_chave_publica_invalida(cliente):
    r = cliente.post("/organizacoes/rascunho", json={
        "cnpj": cnpj_ficticio("998887770001"), "razao_social": "X", "chave_id": "k",
        "chave_publica": "nao-e-chave"})
    assert r.status_code == 400


def test_nao_encontrado_vira_404(api):
    assert aceitar(api, "of_inexistente").status_code == 404
    r = api["cliente"].post("/mandatos/rascunho", json={
        "cnpj": CNPJ_TRANSP, "agente_id": "ag_x", "limites": LIMITES})
    assert r.status_code == 404


def test_corpo_invalido_vira_422(api):
    corpo = corpo_oferta(api)
    corpo["condicoes"]["valor_centavos"] = 48.5  # sem casa decimal
    assert api["cliente"].post("/ofertas/rascunho", json=corpo).status_code == 422
    r = api["cliente"].post("/pedidos", json={"cnpj_solicitante": CNPJ_EMBARC,
                                              "canal": "email", "carga": CARGA,
                                              "chave_privada": "x"})
    assert r.status_code == 422  # campos extras são recusados


def test_float_no_objeto_assinado_vira_400(api):
    r = api["cliente"].post("/ofertas", json={"tipo": "oferta", "valor": 1.5})
    assert r.status_code == 400


def test_so_o_destinatario_aceita(api):
    _, emitida = emitir(api)
    r = aceitar(api, emitida["oferta"]["id"], cnpj_ficticio("334445550001"))
    assert r.status_code == 400 and "destinatário" in r.json()["detail"]


# ---------- página de verificação ----------

def test_pagina_de_compromisso_inexistente(cliente):
    r = cliente.get("/verificar/cp_naoexiste")
    assert r.status_code == 404
    assert "COMPROMISSO INVÁLIDO" in r.text
    assert len(status_na_pagina(r.text)) == 14
    assert status_na_pagina(r.text)[0] == "OK"  # o registro em si está íntegro
    assert cliente.get("/compromissos/cp_naoexiste/verificacao").status_code == 404


def test_pagina_escapa_html(api):
    _, emitida = emitir(api, 900_000)
    oferta_id = emitida["oferta"]["id"]
    _, r = assinar_e_enviar(api["cliente"], f"/ofertas/{oferta_id}/aprovacao/rascunho",
                            {"aprovado_por": "<script>alert(1)</script>"},
                            api["chave_org"], f"/ofertas/{oferta_id}/aprovacao")
    cp_id = aceitar(api, oferta_id).json()["id"]
    texto = api["cliente"].get(f"/verificar/{cp_id}").text
    assert "<script>" not in texto and "&lt;script&gt;" in texto


def test_pagina_denuncia_adulteracao(api, plataforma):
    _, emitida = emitir(api)
    cp_id = aceitar(api, emitida["oferta"]["id"]).json()["id"]
    conexao = plataforma.registro.conexao
    conexao.execute("DROP TRIGGER eventos_sem_update")
    seq, payload = conexao.execute(
        "SELECT seq, payload FROM eventos WHERE tipo = 'oferta_emitida'").fetchone()
    conexao.execute("UPDATE eventos SET payload = ? WHERE seq = ?",
                    (payload.replace("485000", "185000"), seq))
    conexao.commit()
    r = api["cliente"].get(f"/verificar/{cp_id}")
    assert "COMPROMISSO INVÁLIDO" in r.text
    estados = status_na_pagina(r.text)
    assert len(estados) == 14 and estados[0] == "FALHA"


# ---------- app padrão e script de demo ----------

def test_app_padrao_abre_banco_de_elo_db(tmp_path, monkeypatch):
    caminho = tmp_path / "api.db"
    monkeypatch.setenv("ELO_DB", str(caminho))
    with TestClient(criar_app()) as c:  # "with" roda o ciclo de vida (abre o banco)
        assert c.get("/verificar/cp_x").status_code == 404
    assert caminho.exists()


def test_script_de_demo_roda_contra_a_api(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ELO_DB", str(tmp_path / "demo.db"))
    with TestClient(criar_app()) as c:  # relógio real, como no uso de verdade
        compromissos = demo_api.executar(c)
        assert len(compromissos) == 2
        for cid in compromissos:
            assert c.get(f"/compromissos/{cid}/verificacao").json()["valido"]
            assert status_na_pagina(c.get(f"/verificar/{cid}").text) == ["OK"] * 14
    assert "aguardando_aprovacao" in capsys.readouterr().out
