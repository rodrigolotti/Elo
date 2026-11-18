"""Extrai a Tabela A do piso mínimo de frete do texto oficial da ANTT.

Uso (para conferir ou para atualizar elo/antt.py quando a ANTT reajustar):

    python scripts\\extrair_tabela_antt.py docs\\fontes\\antt\\portaria-suroc-22-2026.html

Lê o HTML da portaria publicada no ANTTlegis (o mesmo texto do DOU), acha a
"TABELA A - TRANSPORTE RODOVIÁRIO DE CARGA LOTAÇÃO..." e devolve, para cada tipo
de carga e número de eixos, o par (CCD em décimos de milésimo de real por km,
CC em centavos). Células vazias na norma viram None. Tudo em inteiros: o Elo
não usa números com casa decimal.

O teste tests/test_antt.py roda este extrator sobre o HTML guardado em
docs/fontes/antt e exige que o resultado seja igual à tabela de elo/antt.py.
"""

from __future__ import annotations

import html
import pprint
import re
import sys
from pathlib import Path

EIXOS = (2, 3, 4, 5, 6, 7, 9)

# nome na norma -> identificador no Elo (os cinco primeiros iguais aos do
# projeto esse-frete-paga)
TIPOS = {
    "Carga Geral": "carga-geral",
    "Granel sólido": "granel-solido",
    "Granel líquido": "granel-liquido",
    "Frigorificada ou Aquecida": "frigorificada",
    "Conteinerizada": "conteinerizada",
    "Neogranel": "neogranel",
    "Perigosa (granel sólido)": "perigosa-granel-solido",
    "Perigosa (granel líquido)": "perigosa-granel-liquido",
    "Perigosa (frigorificada ou aquecida)": "perigosa-frigorificada",
    "Perigosa (conteinerizada)": "perigosa-conteinerizada",
    "Perigosa (carga geral)": "perigosa-carga-geral",
    "Carga Granel Pressurizada": "granel-pressurizada",
}

TITULO_TABELA_A = ("TABELA A - TRANSPORTE RODOVIÁRIO DE CARGA LOTAÇÃO EM OPERAÇÕES EM QUE "
                   "HAJA A CONTRATAÇÃO DA COMPOSIÇÃO VEICULAR OU CAMINHÃO SIMPLES")


class ErroExtracao(ValueError):
    pass


def _texto(fragmento: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragmento))).strip()


def _inteiro(valor: str, casas: int) -> int | None:
    """'1.067,06' com 2 casas -> 106706; '5,9180' com 4 casas -> 59180; '' -> None."""
    if valor == "":
        return None
    casa = re.fullmatch(r"(\d{1,3}(?:\.\d{3})*),(\d+)", valor)
    if not casa or len(casa.group(2)) != casas:
        raise ErroExtracao(f"valor fora do formato esperado ({casas} casas): {valor!r}")
    return int(casa.group(1).replace(".", "") + casa.group(2))


def ler_tabela_a(conteudo_html: str) -> dict[str, dict[int, tuple[int | None, int | None]]]:
    maiusculo = conteudo_html.upper()
    inicio, fim = maiusculo.find("TABELA A -"), maiusculo.find("TABELA B -")
    if inicio < 0 or fim < inicio:
        raise ErroExtracao("não achei a Tabela A no documento")
    trecho = conteudo_html[inicio:fim]
    titulo = _texto(trecho[:trecho.find("<")] if "<" in trecho else trecho)
    if titulo.upper() != TITULO_TABELA_A.upper():
        raise ErroExtracao(f"título da Tabela A diferente do esperado: {titulo!r}")

    linhas = []
    for tr in re.findall(r"<tr.*?</tr>", trecho, re.S | re.I):
        linhas.append([_texto(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)])
    cabecalho = next((l for l in linhas if l[3:] == [str(e) for e in EIXOS]), None)
    if cabecalho is None:
        raise ErroExtracao("cabeçalho de eixos diferente de 2, 3, 4, 5, 6, 7, 9")

    tabela: dict[str, dict[int, tuple[int | None, int | None]]] = {}
    for i, linha in enumerate(linhas):
        if len(linha) == 10 and linha[1] == "Deslocamento (CCD)":
            nome, cc = linha[0], linhas[i + 1]
            if nome not in TIPOS:
                raise ErroExtracao(f"tipo de carga novo na norma: {nome!r}")
            if cc[1] != "Carga e descarga (CC)" or len(cc) != 10:
                raise ErroExtracao(f"linha de CC fora do lugar depois de {nome!r}")
            tabela[TIPOS[nome]] = {
                eixo: (_inteiro(linha[3 + k], 4), _inteiro(cc[3 + k], 2))
                for k, eixo in enumerate(EIXOS)
            }
            for eixo, (ccd, c) in tabela[TIPOS[nome]].items():
                if (ccd is None) != (c is None):
                    raise ErroExtracao(f"{nome}, {eixo} eixos: só um dos coeficientes publicado")
    if set(tabela) != set(TIPOS.values()):
        raise ErroExtracao(f"faltam tipos na Tabela A: {set(TIPOS.values()) - set(tabela)}")
    return tabela


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) != 2:
        print(__doc__)
        return 2
    conteudo = Path(argv[1]).read_bytes().decode("latin-1")
    pprint.pprint(ler_tabela_a(conteudo), width=100, sort_dicts=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
