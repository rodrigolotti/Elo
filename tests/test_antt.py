"""Piso mínimo da ANTT (Tabela A): fonte oficial, conta em inteiros e
compatibilidade com o projeto esse-frete-paga."""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import pytest

from elo.antt import EIXOS, TABELAS, TIPOS_CARGA, ErroPiso, piso_centavos, tabela_vigente

RAIZ = Path(__file__).resolve().parents[1]
DADOS_EFP = RAIZ / "tests" / "dados" / "esse_frete_paga.json"
_spec = importlib.util.spec_from_file_location("extrator", RAIZ / "scripts" / "extrair_tabela_antt.py")
extrator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extrator)

TABELA = TABELAS[-1]
BRT = timezone(timedelta(hours=-3))


def _texto_oficial():
    """O texto da portaria, sem as marcas de HTML e com os acentos decodificados."""
    return extrator._texto((RAIZ / TABELA.arquivo_fonte).read_bytes().decode("latin-1"))


# ---------- a tabela é a do texto oficial ----------

def test_fonte_oficial_guardada_e_intacta():
    fonte = RAIZ / TABELA.arquivo_fonte
    assert hashlib.sha256(fonte.read_bytes()).hexdigest() == TABELA.sha256_fonte
    assert TABELA.url.startswith("https://anttlegis.antt.gov.br/")
    texto = _texto_oficial()
    assert "PORTARIA SUROC Nº 22, DE 28 DE SETEMBRO DE 2026" in texto
    assert "D.O.U., 29/09/2026 - Edição Extra 1 A" in texto
    assert "Resolução nº 5.867, de 14 de janeiro de 2020" in texto
    assert "R$ 7,33 por litro" in texto and TABELA.diesel_centavos_por_litro == 733
    assert "entra em vigor no dia seguinte ao de sua publicação" in texto


def test_tabela_do_codigo_e_igual_a_extraida_do_texto_oficial():
    oficial = extrator.ler_tabela_a((RAIZ / TABELA.arquivo_fonte).read_bytes().decode("latin-1"))
    assert TABELA.coeficientes == oficial
    assert list(TABELA.coeficientes) == list(TIPOS_CARGA) or set(TABELA.coeficientes) == set(TIPOS_CARGA)
    assert len(TIPOS_CARGA) == 12


def test_tabela_a_e_de_carga_lotacao():
    assert extrator.TITULO_TABELA_A.upper() in _texto_oficial().upper()
    assert "CARGA LOTAÇÃO" in extrator.TITULO_TABELA_A


@pytest.mark.parametrize("valor, casas, esperado", [
    ("4,0738", 4, 40738), ("1.067,06", 2, 106706), ("597,00", 2, 59700), ("", 2, None)])
def test_extrator_le_numeros_em_inteiros(valor, casas, esperado):
    assert extrator._inteiro(valor, casas) == esperado


@pytest.mark.parametrize("valor, casas", [("4,07", 4), ("451,8", 2), ("4.0738", 4), ("x", 2)])
def test_extrator_recusa_numero_fora_do_formato(valor, casas):
    with pytest.raises(extrator.ErroExtracao):
        extrator._inteiro(valor, casas)


def test_extrator_recusa_documento_sem_tabela_a():
    with pytest.raises(extrator.ErroExtracao, match="não achei a Tabela A"):
        extrator.ler_tabela_a("<html>outro documento</html>")


# ---------- a conta ----------

@pytest.mark.parametrize("tipo, eixos, km, centavos", [
    ("carga-geral", 2, 500, 248_874),     # 500 × 4,0738 + 451,84 = 2.488,74
    ("carga-geral", 5, 500, 407_176),     # 500 × 6,8284 + 657,56 = 4.071,76
    ("granel-pressurizada", 9, 1, 106_221),  # 1 × 9,9531 = 9,95 + 1.052,26
    ("frigorificada", 9, 333, 477_931),   # 333 × 11,1479 = 3.712,2507 → 3.712,25 + 1.067,06
])
def test_piso_em_centavos(tipo, eixos, km, centavos):
    assert piso_centavos(tipo, eixos, km, TABELA) == centavos


def test_celula_vazia_da_norma_nao_tem_piso():
    assert piso_centavos("conteinerizada", 2, 100, TABELA) is None
    assert piso_centavos("granel-pressurizada", 4, 100, TABELA) is None
    assert piso_centavos("granel-pressurizada", 5, 100, TABELA) is not None


@pytest.mark.parametrize("tipo, eixos, km", [("carga geral", 2, 100), ("carga-geral", 8, 100),
                                             ("carga-geral", 2, 0), ("carga-geral", 2, 10.5)])
def test_dados_fora_da_tabela_sao_recusados(tipo, eixos, km):
    with pytest.raises(ErroPiso):
        piso_centavos(tipo, eixos, km, TABELA)


def test_tabela_vigente_pela_data_em_brasilia():
    assert tabela_vigente(datetime(2026, 9, 30, 0, 0, tzinfo=BRT)) is TABELA
    assert tabela_vigente(datetime(2026, 11, 18, 12, 0, tzinfo=BRT)) is TABELA
    # 30/09 às 02:59 em UTC ainda é 29/09 às 23:59 em Brasília: antes da vigência
    assert tabela_vigente(datetime(2026, 9, 30, 2, 59, tzinfo=timezone.utc)) is None
    assert tabela_vigente(datetime(2026, 9, 29, 23, 59, tzinfo=BRT)) is None


# ---------- compatibilidade com o esse-frete-paga ----------

def _dados_efp():
    return json.loads(DADOS_EFP.read_text(encoding="utf-8"))


def _centavos_efp(valor):
    return None if valor is None else round(valor * 10_000)


def test_coeficientes_do_esse_frete_paga_batem_com_a_tabela_oficial():
    dados = _dados_efp()
    assert dados["eixos"] == list(EIXOS)
    diferencas = []
    for tipo, linha in dados["tabelaA"].items():
        for k, eixos in enumerate(EIXOS):
            ccd_efp = _centavos_efp(linha["ccd"][k])
            cc_efp = None if linha["cc"][k] is None else round(linha["cc"][k] * 100)
            if (ccd_efp, cc_efp) != TABELA.coeficientes[tipo][eixos]:
                diferencas.append((tipo, eixos, (ccd_efp, cc_efp), TABELA.coeficientes[tipo][eixos]))
    assert diferencas == [], f"esse-frete-paga diverge da tabela oficial: {diferencas}"


# Onde a conta exata dá meio centavo, o Elo arredonda para cima (conta em
# inteiros) e o esse-frete-paga pode cair 1 centavo abaixo, porque em float
# 10 × 5,2555 vira 52,55499999… Fora desses casos, os dois têm de ser iguais.
MEIO_CENTAVO_CONHECIDOS = [("granel-solido", 3, 10)]  # exato: 604,795 → Elo 604,80; efp 604,79


def _comparar_com_efp(pisos):
    meio_centavo = []
    for caso in pisos:
        chave = (caso["tipoCarga"], caso["eixos"], caso["distanciaKm"])
        elo = piso_centavos(*chave, TABELA)
        efp = caso["pisoCentavos"]
        if elo == efp:
            continue
        ccd, cc = TABELA.coeficientes[chave[0]][chave[1]]
        exato = Fraction(chave[2] * ccd, 100) + cc
        assert exato.denominator == 2 and efp == elo - 1, (
            f"diferença que não é de meio centavo: {chave} elo={elo} efp={efp} exato={exato}")
        meio_centavo.append(chave)
    return meio_centavo


def test_esse_frete_paga_e_elo_dao_o_mesmo_piso():
    dados = _dados_efp()
    assert len(dados["pisos"]) == 5 * 7 * 10
    assert _comparar_com_efp(dados["pisos"]) == MEIO_CENTAVO_CONHECIDOS


def test_esse_frete_paga_ao_vivo():
    """Roda o código do esse-frete-paga agora, se ESSE_FRETE_PAGA apontar para ele."""
    pasta = os.environ.get("ESSE_FRETE_PAGA")
    if not pasta or shutil.which("node") is None:
        pytest.skip("defina ESSE_FRETE_PAGA com a pasta do projeto (e tenha Node)")
    r = subprocess.run(["node", str(RAIZ / "scripts" / "pisos_esse_frete_paga.mjs"), pasta],
                       capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert _comparar_com_efp(json.loads(r.stdout.decode("utf-8"))["pisos"])         == MEIO_CENTAVO_CONHECIDOS
