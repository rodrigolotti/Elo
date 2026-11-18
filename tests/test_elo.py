import pytest

from conftest import CNPJ_EMBARC, CNPJ_TRANSP, cnpj_ficticio, condicoes
from elo.canonico import ErroCanonico, canonicalizar, hash_sha256
from elo.chaves import ParDeChaves, verificar_assinatura
from elo.cnpj import validar_cnpj
from elo.modelos import LimitesMandato
from elo.registro import Registro
from elo.servico import ErroElo, Plataforma

VALIDADE = "2026-11-18T18:00:00-03:00"


# ---------- canonicalização ----------

def test_canonico_ordena_chaves_e_remove_espacos():
    assert canonicalizar({"b": 1, "a": [True, None, "x"]}) == b'{"a":[true,null,"x"],"b":1}'


def test_canonico_mesmo_objeto_mesmos_bytes_independente_da_ordem():
    assert canonicalizar({"x": 1, "y": {"b": 2, "a": 3}}) == canonicalizar({"y": {"a": 3, "b": 2}, "x": 1})


def test_canonico_escapa_controle_e_mantem_acentos():
    assert canonicalizar({"t": "ação\n\u0001"}) == '{"t":"ação\\n\\u0001"}'.encode()


def test_canonico_recusa_float():
    with pytest.raises(ErroCanonico):
        canonicalizar({"valor": 48.5})


# ---------- CNPJ e assinatura ----------

def test_cnpj():
    assert validar_cnpj(CNPJ_TRANSP)
    assert not validar_cnpj("11.111.111/1111-11")
    assert not validar_cnpj(CNPJ_TRANSP[:-1] + ("0" if CNPJ_TRANSP[-1] != "0" else "1"))


def test_assinatura_detecta_qualquer_alteracao():
    chave = ParDeChaves.gerar("k1")
    assinado = chave.assinar({"valor_centavos": 485000})
    assert verificar_assinatura(assinado, chave.publica_b64)
    adulterado = {**assinado, "valor_centavos": 100}
    assert not verificar_assinatura(adulterado, chave.publica_b64)
    assert not verificar_assinatura(assinado, ParDeChaves.gerar("k2").publica_b64)


# ---------- fluxo principal ----------

def test_fluxo_dentro_do_mandato_gera_compromisso_valido(cenario):
    p = cenario["plataforma"]
    oferta, problemas, token = p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"],
                                        cenario["mandato_id"], cenario["chave_agente"],
                                        condicoes(), VALIDADE)
    assert problemas == []
    assert p.status_oferta[oferta["id"]] == "enviada"
    cenario["relogio"].avancar(hours=2)
    cp = p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria Exemplo", "maria@exemplo.com", token)
    relatorio = p.verificar_compromisso(cp["id"])
    assert relatorio.valido, [v for v in relatorio.verificacoes if not v.ok]


def test_oferta_acima_do_limite_exige_aprovacao_humana(cenario):
    p = cenario["plataforma"]
    oferta, problemas, token = p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"],
                                        cenario["mandato_id"], cenario["chave_agente"],
                                        condicoes(valor_centavos=900_000), VALIDADE)
    assert any("acima do limite" in x for x in problemas)
    with pytest.raises(ErroElo):
        p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)
    p.aprovar_oferta(oferta["id"], cenario["chave_org"], "Gerente Comercial")
    cp = p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)
    relatorio = p.verificar_compromisso(cp["id"])
    assert relatorio.valido
    autorizacao = next(v for v in relatorio.verificacoes if v.nome == "Oferta autorizada")
    assert "aprovada por Gerente Comercial" in autorizacao.detalhe


def test_aprovacao_exige_chave_da_organizacao(cenario):
    p = cenario["plataforma"]
    oferta, _, token = p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"],
                                cenario["mandato_id"], cenario["chave_agente"],
                                condicoes(valor_centavos=900_000), VALIDADE)
    with pytest.raises(ErroElo):
        p.aprovar_oferta(oferta["id"], ParDeChaves.gerar("intruso"), "Intruso")


def test_mandato_revogado_bloqueia_novas_ofertas(cenario):
    p = cenario["plataforma"]
    p.revogar_mandato(cenario["mandato_id"], cenario["chave_org"])
    cenario["relogio"].avancar(minutes=1)
    _, problemas, _ = p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"],
                                   cenario["mandato_id"], cenario["chave_agente"],
                                   condicoes(), VALIDADE)
    assert any("revogado" in x for x in problemas)


def test_revogacao_no_mesmo_segundo_vale_pela_ordem_do_registro(cenario):
    """Sem avançar o relógio: oferta, aceite e revogação no mesmo segundo."""
    p = cenario["plataforma"]
    oferta, problemas, token = p.emitir_oferta(
        cenario["pedido_id"], cenario["agente_id"], cenario["mandato_id"],
        cenario["chave_agente"], condicoes(), VALIDADE)
    assert problemas == []
    cp = p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)
    p.revogar_mandato(cenario["mandato_id"], cenario["chave_org"])
    # a oferta entrou no registro antes da revogação: continua autorizada
    assert p.verificar_compromisso(cp["id"]).valido
    # a oferta seguinte, no mesmo segundo, já sai do mandato
    _, problemas, _ = p.emitir_oferta(
        cenario["pedido_id"], cenario["agente_id"], cenario["mandato_id"],
        cenario["chave_agente"], condicoes(), VALIDADE)
    assert any("revogado" in x for x in problemas)


def test_oferta_vencida_nao_pode_ser_aceita(cenario):
    p = cenario["plataforma"]
    oferta, _, token = p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"],
                                cenario["mandato_id"], cenario["chave_agente"],
                                condicoes(), VALIDADE)
    cenario["relogio"].avancar(days=3)
    with pytest.raises(ErroElo, match="vencida"):
        p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)


def test_so_o_destinatario_pode_aceitar(cenario):
    from conftest import cnpj_ficticio
    p = cenario["plataforma"]
    oferta, _, token = p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"],
                                cenario["mandato_id"], cenario["chave_agente"],
                                condicoes(), VALIDADE)
    with pytest.raises(ErroElo, match="destinatário"):
        p.aceitar_oferta(oferta["id"], cnpj_ficticio("334445550001"), "Outro", "o@x.com", token)


def test_oferta_nao_pode_ser_aceita_duas_vezes(cenario):
    p = cenario["plataforma"]
    oferta, _, token = p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"],
                                cenario["mandato_id"], cenario["chave_agente"],
                                condicoes(), VALIDADE)
    p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)
    with pytest.raises(ErroElo):
        p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)


def test_agente_nao_assina_com_chave_alheia(cenario):
    p = cenario["plataforma"]
    with pytest.raises(ErroElo):
        p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"], cenario["mandato_id"],
                        ParDeChaves.gerar("falsa"), condicoes(), VALIDADE)


def test_limites_rejeitam_periodo_invertido():
    with pytest.raises(ValueError):
        LimitesMandato(valor_max_centavos=1, ufs_origem=["SP"], ufs_destino=["SP"],
                       tipos_carga=["x"], valido_de="2026-12-01T00:00:00-03:00",
                       valido_ate="2026-11-01T00:00:00-03:00")


# ---------- registro ----------

def test_registro_recusa_update_e_delete(cenario):
    conexao = cenario["plataforma"].registro.conexao
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError, match="só de inclusão"):
        conexao.execute("UPDATE eventos SET tipo = 'x' WHERE seq = 1")
    with pytest.raises(sqlite3.DatabaseError, match="só de inclusão"):
        conexao.execute("DELETE FROM eventos WHERE seq = 1")


def test_adulteracao_direta_no_banco_e_detectada(cenario):
    p = cenario["plataforma"]
    oferta, _, token = p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"],
                                cenario["mandato_id"], cenario["chave_agente"],
                                condicoes(), VALIDADE)
    cp = p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)
    assert p.verificar_compromisso(cp["id"]).valido

    # simula alguém com acesso ao arquivo: remove os gatilhos e altera o valor
    conexao = p.registro.conexao
    conexao.execute("DROP TRIGGER eventos_sem_update")
    linha = conexao.execute(
        "SELECT seq, payload FROM eventos WHERE tipo = 'oferta_emitida'").fetchone()
    conexao.execute("UPDATE eventos SET payload = ? WHERE seq = ?",
                    (linha[1].replace("485000", "185000"), linha[0]))
    conexao.commit()

    resultado = p.registro.verificar_integridade()
    assert not resultado.integro
    assert resultado.primeiro_evento_com_problema == linha[0]

    relatorio = p.verificar_compromisso(cp["id"])
    assert not relatorio.valido
    falhas = {v.nome for v in relatorio.verificacoes if not v.ok}
    assert "Oferta aceita encontrada pelo hash" in falhas


def test_estado_e_reconstruido_a_partir_do_registro(cenario, relogio, tmp_path):
    p = cenario["plataforma"]
    oferta, _, token = p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"],
                                cenario["mandato_id"], cenario["chave_agente"],
                                condicoes(), VALIDADE)
    cp = p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)
    # nova instância lendo o mesmo banco, com a mesma chave de plataforma
    nova = Plataforma(p.registro, p.chave, relogio)
    assert nova.verificar_compromisso(cp["id"]).valido
    assert nova.status_oferta[oferta["id"]] == "aceita"


def test_registro_em_arquivo_persiste(tmp_path, relogio):
    caminho = str(tmp_path / "elo.db")
    chave = ParDeChaves.gerar("plataforma#k1")
    p = Plataforma(Registro(caminho), chave, relogio)
    from conftest import cnpj_ficticio
    p.cadastrar_organizacao(cnpj_ficticio("998887770001"), "Teste", ParDeChaves.gerar("o"))
    p.registro.conexao.close()
    reaberto = Plataforma(Registro(caminho), chave, relogio)
    assert len(reaberto.organizacoes) == 1
    assert reaberto.registro.verificar_integridade().integro


# ---------- recusa de oferta ----------

def _fora_do_mandato(cenario):
    p = cenario["plataforma"]
    oferta, problemas, token = p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"],
                                               cenario["mandato_id"], cenario["chave_agente"],
                                               condicoes(valor_centavos=900_000), VALIDADE)
    assert problemas
    return oferta, token


def test_oferta_recusada_nao_pode_ser_aprovada_nem_aceita(cenario):
    p = cenario["plataforma"]
    oferta, token = _fora_do_mandato(cenario)
    recusa = p.recusar_oferta(oferta["id"], cenario["chave_org"], "Gerente Comercial",
                              "valor acima da tabela")
    assert recusa["tipo"] == "recusa" and recusa["motivo"] == "valor acima da tabela"
    assert verificar_assinatura(recusa, p.organizacoes[CNPJ_TRANSP]["chave_publica"])
    assert p.status_oferta[oferta["id"]] == "recusada"
    with pytest.raises(ErroElo, match="recusada"):
        p.aprovar_oferta(oferta["id"], cenario["chave_org"], "Gerente Comercial")
    with pytest.raises(ErroElo, match="recusada"):
        p.recusar_oferta(oferta["id"], cenario["chave_org"], "Gerente Comercial")
    with pytest.raises(ErroElo, match="recusada"):
        p.aceitar_oferta(oferta["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)


def test_recusa_fica_no_registro_e_sobrevive_ao_reinicio(cenario):
    p = cenario["plataforma"]
    oferta, _ = _fora_do_mandato(cenario)
    p.recusar_oferta(oferta["id"], cenario["chave_org"], "Gerente Comercial")
    assert [e.tipo for e in p.registro.eventos()][-1] == "oferta_recusada"
    p._reconstruir()
    assert p.status_oferta[oferta["id"]] == "recusada"
    assert p.registro.verificar_integridade().integro


def test_recusa_exige_chave_da_organizacao(cenario):
    p = cenario["plataforma"]
    oferta, _ = _fora_do_mandato(cenario)
    for intruso in (ParDeChaves.gerar("intruso"), cenario["chave_agente"]):
        with pytest.raises(ErroElo):
            p.recusar_oferta(oferta["id"], intruso, "Intruso")
    assert p.status_oferta[oferta["id"]] == "aguardando_aprovacao"


def test_so_oferta_aguardando_aprovacao_pode_ser_recusada(cenario):
    p = cenario["plataforma"]
    oferta, _, _ = p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"],
                                   cenario["mandato_id"], cenario["chave_agente"],
                                   condicoes(), VALIDADE)
    with pytest.raises(ErroElo, match="enviada"):
        p.recusar_oferta(oferta["id"], cenario["chave_org"], "Gerente Comercial")


def test_aprovacao_gravada_depois_da_recusa_nao_vale(cenario):
    # cenário de conluio: alguém com acesso ao banco e à chave da organização
    # grava uma aprovação e um aceite por fora das regras, depois da recusa
    p = cenario["plataforma"]
    oferta, _ = _fora_do_mandato(cenario)
    p.recusar_oferta(oferta["id"], cenario["chave_org"], "Gerente Comercial")
    oferta_hash = hash_sha256(oferta)
    p._incluir("oferta_aprovada", cenario["chave_org"].assinar({
        "tipo": "aprovacao", "oferta_hash": oferta_hash, "aprovado_por": "Alguém",
        "em": p._agora_iso()}), oferta["id"])
    assert p.status_oferta[oferta["id"]] == "recusada"
    cp = p.chave.assinar({"tipo": "compromisso", "id": "cp_forjado",
                          "oferta_hash": oferta_hash,
                          "aceite": {"cnpj": CNPJ_EMBARC, "nome": "Maria",
                                     "email": "maria@exemplo.com", "modo": "humano_via_link",
                                     "em": p._agora_iso()}})
    p._incluir("compromisso_firmado", cp, cp["id"])
    relatorio = p.verificar_compromisso("cp_forjado")
    assert not relatorio.valido
    autorizacao = next(v for v in relatorio.verificacoes if v.nome == "Oferta autorizada")
    assert not autorizacao.ok and "recusada pela organização" in autorizacao.detalhe


# ---------- consultas do painel ----------

def test_consultas_so_trazem_a_propria_organizacao(cenario):
    p = cenario["plataforma"]
    dentro, _, token = p.emitir_oferta(cenario["pedido_id"], cenario["agente_id"],
                                       cenario["mandato_id"], cenario["chave_agente"],
                                       condicoes(), VALIDADE)
    fora, _ = _fora_do_mandato(cenario)
    cp = p.aceitar_oferta(dentro["id"], CNPJ_EMBARC, "Maria", "maria@exemplo.com", token)

    pendentes = p.listar_ofertas(CNPJ_TRANSP, "aguardando_aprovacao")
    assert [o["oferta"]["id"] for o in pendentes] == [fora["id"]]
    assert any("acima do limite" in v for v in pendentes[0]["violacoes"])
    assert {o["status"] for o in p.listar_ofertas(CNPJ_TRANSP)} == {"aceita",
                                                                    "aguardando_aprovacao"}
    [compromisso] = p.listar_compromissos(CNPJ_TRANSP)
    assert compromisso["verificacao"] == f"/verificar/{cp['id']}"
    [agente] = p.listar_agentes(CNPJ_TRANSP)
    assert agente["agente_id"] == cenario["agente_id"]
    [mandato] = p.listar_mandatos(CNPJ_TRANSP)
    assert mandato["revogado_em"] is None and mandato["agente_nome"] == "Agente de cotação"

    # outra organização não vê nada da primeira
    outra = cnpj_ficticio("998887770001")
    p.cadastrar_organizacao(outra, "Outra Transportadora Ltda", ParDeChaves.gerar("outra"))
    assert p.listar_ofertas(outra) == [] and p.listar_compromissos(outra) == []
    assert p.listar_agentes(outra) == [] and p.listar_mandatos(outra) == []

    p.revogar_mandato(cenario["mandato_id"], cenario["chave_org"])
    assert p.listar_mandatos(CNPJ_TRANSP)[0]["revogado_em"] is not None
