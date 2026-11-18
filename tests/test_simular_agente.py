"""scripts/simular_agente.py: chave do agente, oferta e aceite, só pela API."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import CNPJ_EMBARC, CNPJ_TRANSP, cnpj_ficticio
from elo.chaves import ParDeChaves, verificar_assinatura
from test_api import LIMITES, assinar_e_enviar, cliente, plataforma  # noqa: F401  (fixtures)

RAIZ = Path(__file__).resolve().parents[1]
SCRIPT = RAIZ / "scripts" / "simular_agente.py"
_spec = importlib.util.spec_from_file_location("simular_agente", SCRIPT)
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)


@pytest.fixture
def pasta(tmp_path):
    return tmp_path / "chaves_locais"


@pytest.fixture
def painel(cliente, pasta):
    """O que a pessoa faz no painel: cadastra a transportadora, o agente (com a
    chave pública impressa pelo simulador) e um mandato."""
    chave_org = ParDeChaves.gerar("transp#k1")
    _, r = assinar_e_enviar(cliente, "/organizacoes/rascunho", {
        "cnpj": CNPJ_TRANSP, "razao_social": "Transportadora Fictícia Ltda",
        "chave_id": chave_org.chave_id, "chave_publica": chave_org.publica_b64,
    }, chave_org, "/organizacoes")
    assert r.status_code == 201, r.text
    criada = sim.criar_chave(pasta)
    _, r = assinar_e_enviar(cliente, "/agentes/rascunho", {
        "cnpj": CNPJ_TRANSP, "nome": "Agente simulado",
        "chave_id": criada["chave_id"], "chave_publica": criada["chave_publica"],
    }, chave_org, "/agentes")
    assert r.status_code == 201, r.text
    agente_id = r.json()["agente_id"]
    _, r = assinar_e_enviar(cliente, "/mandatos/rascunho", {
        "cnpj": CNPJ_TRANSP, "agente_id": agente_id, "limites": LIMITES,
    }, chave_org, "/mandatos")
    assert r.status_code == 201, r.text
    return {"cliente": cliente, "chave_org": chave_org, "agente_id": agente_id,
            "mandato_id": r.json()["id"], "pasta": pasta}


def oferta(painel, valor="4850", **extra):
    args = {"cnpj": CNPJ_TRANSP, "mandato": painel["mandato_id"], "valor": valor,
            "pasta": painel["pasta"], "embarcador": CNPJ_EMBARC, **extra}
    return sim.emitir_oferta(painel["cliente"], **args)


# ---------- chave ----------

def test_chave_fica_num_arquivo_local_e_bate_com_a_publica(pasta):
    criada = sim.criar_chave(pasta)
    dados = json.loads((pasta / "agente.json").read_text(encoding="utf-8"))
    assert dados["chave_publica"] == criada["chave_publica"] and dados["agente_id"] is None
    par = sim._par(dados)
    assert verificar_assinatura(par.assinar({"x": 1}), criada["chave_publica"])


def test_chave_nao_e_sobrescrita_sem_pedir(pasta):
    primeira = sim.criar_chave(pasta)
    with pytest.raises(sim.ErroSimulador, match="--substituir"):
        sim.criar_chave(pasta)
    segunda = sim.criar_chave(pasta, substituir=True)
    assert segunda["chave_publica"] != primeira["chave_publica"]


def test_pasta_das_chaves_e_ignorada_pelo_git():
    if shutil.which("git") is None or not (RAIZ / ".git").exists():
        pytest.skip("sem git")
    r = subprocess.run(["git", "check-ignore", "-q", "chaves_locais/agente.json"], cwd=RAIZ)
    assert r.returncode == 0, "chaves_locais/ precisa estar no .gitignore"
    assert sim.PASTA_PADRAO == RAIZ / "chaves_locais"


# ---------- oferta e aceite ----------

def test_oferta_dentro_do_mandato_e_aceite(painel):
    r = oferta(painel, agente=painel["agente_id"])
    assert r["status"] == "enviada" and r["violacoes"] == [] and r["token"]
    assert r["oferta"]["condicoes"]["valor_centavos"] == 485_000
    cp = sim.aceitar(painel["cliente"], oferta=r["oferta"]["id"], token=r["token"],
                     embarcador=CNPJ_EMBARC)
    rel = painel["cliente"].get(f"/compromissos/{cp['id']}/verificacao").json()
    assert rel["valido"], rel


def test_agente_fica_lembrado_depois_da_primeira_oferta(painel):
    with pytest.raises(sim.ErroSimulador, match="informe --agente"):
        oferta(painel)
    oferta(painel, agente=painel["agente_id"])
    assert oferta(painel)["status"] == "enviada"  # sem --agente agora


def test_oferta_fora_do_mandato_vai_para_aprovacao(painel):
    r = oferta(painel, valor="9.000,00", agente=painel["agente_id"])
    assert r["status"] == "aguardando_aprovacao"
    assert any("acima do limite" in v for v in r["violacoes"])
    with pytest.raises(sim.ErroSimulador, match="aprove no painel"):
        sim.aceitar(painel["cliente"], oferta=r["oferta"]["id"], token=r["token"],
                    embarcador=CNPJ_EMBARC)


def test_cnpj_de_outra_transportadora_e_recusado(painel):
    with pytest.raises(sim.ErroSimulador, match="este agente é da transportadora"):
        oferta(painel, agente=painel["agente_id"], cnpj=cnpj_ficticio("998887770001"))


def test_sem_chave_explica_o_que_fazer(cliente, tmp_path):
    with pytest.raises(sim.ErroSimulador, match="rode antes"):
        sim.emitir_oferta(cliente, cnpj=CNPJ_TRANSP, mandato="md_x", valor="4850",
                          agente="ag_x", pasta=tmp_path / "vazia")


def test_erro_da_api_vira_mensagem(painel):
    with pytest.raises(sim.ErroSimulador, match="a API recusou \\(404\\)"):
        oferta(painel, agente="ag_naoexiste")


@pytest.mark.parametrize("texto, centavos", [("4850", 485_000), ("4.850,50", 485_050),
                                             ("R$ 4850,5", 485_050), ("0,99", 99)])
def test_valor_em_reais_vira_centavos(texto, centavos):
    assert sim.centavos_de_reais(texto) == centavos


@pytest.mark.parametrize("texto", ["4850.50", "abc", "", "-10"])
def test_valor_invalido_e_recusado(texto):
    with pytest.raises(sim.ErroSimulador, match="valor em reais não reconhecido"):
        sim.centavos_de_reais(texto)


# ---------- linha de comando ----------

def test_linha_de_comando_chave_num_console_cp1252(tmp_path):
    ambiente = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    r = subprocess.run([sys.executable, str(SCRIPT), "--pasta", str(tmp_path), "chave"],
                       cwd=RAIZ, env=ambiente, capture_output=True, timeout=60)
    saida = r.stdout.decode("utf-8")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert "Chave pública do agente:" in saida and "Identificador da chave:" in saida
    r = subprocess.run([sys.executable, str(SCRIPT), "--pasta", str(tmp_path), "chave"],
                       cwd=RAIZ, env=ambiente, capture_output=True, timeout=60)
    assert r.returncode == 1 and "--substituir" in r.stderr.decode("utf-8")


def test_linha_de_comando_sem_api_no_ar(tmp_path):
    sim.criar_chave(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPT), "--url", "http://127.0.0.1:9",
                        "--pasta", str(tmp_path), "oferta", "--cnpj", CNPJ_TRANSP,
                        "--mandato", "md_x", "--valor", "4850", "--agente", "ag_x"],
                       cwd=RAIZ, capture_output=True, timeout=60)
    assert r.returncode == 1
    assert "não consegui falar com a API" in r.stderr.decode("utf-8")
