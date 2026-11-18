"""Painel web: como é servido (CSP, MIME, rotas) e as funções puras do JavaScript."""

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from elo.api import PASTA_PAINEL, _em_brasilia
from elo.chaves import ParDeChaves
from elo.mandato import reais
from elo.modelos import LimitesMandato, ler_momento
from test_api import cliente, plataforma  # noqa: F401  (fixtures)
from test_painel_js import navegador  # noqa: F401  (fixture)

ARQUIVOS_JS = sorted((PASTA_PAINEL / "js").glob("*.js"))


# ---------- como o painel é servido ----------

def test_painel_e_servido_com_csp_restrita(cliente):
    r = cliente.get("/painel/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert '<script type="module" src="/painel/js/app.js">' in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("arquivo", ARQUIVOS_JS, ids=lambda p: p.name)
def test_modulos_js_sao_servidos_como_javascript(cliente, arquivo):
    r = cliente.get(f"/painel/js/{arquivo.name}")
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0] == "text/javascript"
    assert "content-security-policy" in r.headers


def test_css_e_servido(cliente):
    r = cliente.get("/painel/estilo.css")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")


def test_raiz_e_painel_sem_barra_levam_ao_painel(cliente):
    for rota in ("/", "/painel"):
        r = cliente.get(rota, follow_redirects=False)
        assert r.status_code in (301, 302, 307, 308), rota
        assert r.headers["location"].endswith("/painel/")


def test_painel_nao_serve_arquivos_de_fora_da_pasta(cliente):
    for rota in ("/painel/../api.py", "/painel/%2e%2e/api.py", "/painel/js/../../api.py"):
        r = cliente.get(rota)
        assert r.status_code == 404 or "Plataforma" not in r.text, rota


def test_pagina_publica_de_verificacao_nao_usa_a_csp_do_painel(cliente):
    # ela tem <style> inline; a CSP do painel quebraria o visual dela
    r = cliente.get("/verificar/cp_naoexiste")
    assert "content-security-policy" not in r.headers


# ---------- regras do código do painel (CSP e XSS) ----------

def test_index_nao_tem_script_estilo_nem_evento_inline():
    texto = (PASTA_PAINEL / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", texto)
    assert not re.search(r"<style", texto)
    assert not re.search(r"\sstyle=", texto)
    assert not re.search(r"\son[a-z]+=", texto)


@pytest.mark.parametrize("arquivo", ARQUIVOS_JS, ids=lambda p: p.name)
def test_js_nunca_interpreta_texto_como_html_ou_codigo(arquivo):
    texto = arquivo.read_text(encoding="utf-8")
    for proibido in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                     "eval(", "new Function"):
        assert proibido not in texto, f"{arquivo.name} usa {proibido}"


def test_todos_os_imports_e_referencias_existem():
    for arquivo in ARQUIVOS_JS:
        for alvo in re.findall(r'from "(\./[^"]+)"', arquivo.read_text(encoding="utf-8")):
            assert (arquivo.parent / alvo).is_file(), f"{arquivo.name} importa {alvo}"
    index = (PASTA_PAINEL / "index.html").read_text(encoding="utf-8")
    for alvo in re.findall(r'(?:src|href)="/painel/([^"]+)"', index):
        assert (PASTA_PAINEL / alvo).is_file(), alvo


def test_chave_privada_so_e_criada_nao_exportavel():
    # toda geração de chave no painel passa false em "extractable", com UMA
    # exceção: a chave de recuperação, que precisa sair cifrada para o arquivo
    for arquivo in ARQUIVOS_JS:
        texto = arquivo.read_text(encoding="utf-8")
        if arquivo.name == "recuperacao.js":
            assert len(re.findall(r"generateKey\(", texto)) == 1
            assert texto.count('exportKey("pkcs8"') == 1
            continue
        for chamada in re.findall(r"generateKey\(([^)]*)\)", texto):
            assert re.search(r"\},\s*false,", chamada), f"{arquivo.name}: {chamada}"
        assert "exportKey(\"pkcs8\"" not in texto
        assert "exportKey(\"jwk\"" not in texto


# ---------- funções puras, rodando no Node ----------

def chamar(navegador, modulo, funcao, *args):
    r = navegador("chamar", modulo=modulo, funcao=funcao, args=list(args))
    if not r["ok"]:
        raise ValueError(r["erro"])
    return r["valor"]


@pytest.mark.parametrize("centavos", [0, 5, 99, 100, 485_000, 600_000, 123_456_789, 10**15])
def test_reais_igual_ao_python(navegador, centavos):
    assert chamar(navegador, "formatos.js", "reais", centavos) == reais(centavos)


@pytest.mark.parametrize("texto, centavos", [
    ("6.000,00", 600_000), ("6000", 600_000), ("R$ 4.850,50", 485_050), ("4.850,5", 485_050),
    ("0,99", 99), ("1.234.567,89", 123_456_789), (" 12 ", 1200),
])
def test_valor_digitado_vira_centavos_sem_float(navegador, texto, centavos):
    assert chamar(navegador, "formatos.js", "centavosDeReais", texto) == centavos


@pytest.mark.parametrize("texto", ["6,000.00", "1,234", "abc", "", "-5", "1.2345", "6.00,00",
                                   "99999999999999999"])
def test_valor_digitado_invalido_e_recusado(navegador, texto):
    with pytest.raises(ValueError):
        chamar(navegador, "formatos.js", "centavosDeReais", texto)


@pytest.mark.parametrize("iso", ["2026-11-18T00:48:30+00:00", "2026-11-18T18:00:00-03:00",
                                 "2026-12-31T23:59:59-03:00", "2027-01-01T02:59:00Z"])
def test_datas_em_brasilia_iguais_a_pagina_de_verificacao(navegador, iso):
    esperado = _em_brasilia(iso).removesuffix(" (horário de Brasília)")
    assert chamar(navegador, "formatos.js", "emBrasilia", iso) == esperado


def test_data_do_formulario_vira_iso_aceito_pelo_servidor(navegador):
    de = chamar(navegador, "formatos.js", "isoDeBrasilia", "2026-11-01T00:00")
    ate = chamar(navegador, "formatos.js", "isoDeBrasilia", "2026-12-31T23:59")
    assert (de, ate) == ("2026-11-01T00:00:00-03:00", "2026-12-31T23:59:00-03:00")
    assert ler_momento(de) == datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
    LimitesMandato(valor_max_centavos=1, ufs_origem=["SP"], ufs_destino=["PR"],
                   tipos_carga=["carga geral"], valido_de=de, valido_ate=ate)
    with pytest.raises(ValueError):
        chamar(navegador, "formatos.js", "isoDeBrasilia", "01/11/2026 00:00")


def test_formatos_auxiliares(navegador):
    assert chamar(navegador, "formatos.js", "dataBr", "2026-11-20") == "20/11/2026"
    assert chamar(navegador, "formatos.js", "lista", "carga geral, refrigerada\ncarga geral\n ") \
        == ["carga geral", "refrigerada"]
    publica = ParDeChaves.gerar("x").publica_b64
    assert chamar(navegador, "formatos.js", "chavePublicaValida", publica) is True
    for ruim in ("", "abc", publica[:-4], "não é base64!"):
        assert chamar(navegador, "formatos.js", "chavePublicaValida", ruim) is False


# ---------- conferência do rascunho antes de assinar ----------

LIMITES = {"valor_max_centavos": 600_000, "ufs_origem": ["SP"], "ufs_destino": ["SP", "PR"],
           "tipos_carga": ["carga geral"], "valido_de": "2026-11-01T00:00:00-03:00",
           "valido_ate": "2026-12-31T23:59:00-03:00"}
RASCUNHO = {"tipo": "mandato", "id": "md_1", "cnpj": "11.222.333/0001-81", "agente_id": "ag_1",
            "limites": LIMITES, "emitido_em": "2026-11-17T09:00:00-03:00"}


def conferir(navegador, rascunho, esperado):
    return chamar(navegador, "conferencia.js", "conferirRascunho", rascunho, esperado)


def test_rascunho_igual_ao_pedido_passa(navegador):
    assert conferir(navegador, RASCUNHO, {"tipo": "mandato", "agente_id": "ag_1",
                                          "limites": dict(reversed(LIMITES.items()))}) == RASCUNHO


@pytest.mark.parametrize("adulterado", [
    {**RASCUNHO, "agente_id": "ag_2"},
    {**RASCUNHO, "limites": {**LIMITES, "valor_max_centavos": 60_000_000}},
    {**RASCUNHO, "limites": {**LIMITES, "ufs_destino": ["PR", "SP"]}},
    {**RASCUNHO, "limites": {**LIMITES, "tipos_carga": ["carga geral", "perigosa"]}},
    {k: v for k, v in RASCUNHO.items() if k != "agente_id"},
    {**RASCUNHO, "tipo": "revogacao"},
    {**RASCUNHO, "assinatura": {"alg": "Ed25519", "chave_id": "x", "valor": "AA=="}},
], ids=["agente", "valor", "ordem-ufs", "tipos", "sem-campo", "tipo", "ja-assinado"])
def test_rascunho_diferente_do_pedido_nao_e_assinado(navegador, adulterado):
    with pytest.raises(ValueError, match="ErroConferencia: .*nada foi assinado"):
        conferir(navegador, adulterado, {"tipo": "mandato", "agente_id": "ag_1",
                                         "limites": LIMITES})


def test_conferencia_recusa_resposta_que_nao_e_objeto(navegador):
    for resposta in (None, [], "texto"):
        with pytest.raises(ValueError, match="ErroConferencia"):
            conferir(navegador, resposta, {"tipo": "mandato"})


def test_painel_existe_no_pacote():
    assert Path(PASTA_PAINEL, "index.html").is_file()
