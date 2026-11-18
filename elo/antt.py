"""Piso mínimo de frete da ANTT (Política Nacional de Pisos Mínimos, Lei 13.703/2018).

Só a TABELA A: "transporte rodoviário de carga lotação em operações em que haja a
contratação da composição veicular ou caminhão simples". Não vale para carga
fracionada nem para as Tabelas B (só a unidade de tração), C e D (alto desempenho).

    piso = distância (km, só a ida) × CCD + CC

Os coeficientes ficam em inteiros: CCD em décimos de milésimo de real por km
(R$ 4,0738/km -> 40738) e CC em centavos (R$ 451,84 -> 45184). O piso sai em
centavos, arredondado meio para cima: (km × CCD + 50) // 100 + CC.

As tabelas são versionadas por vigência: a checagem de uma oferta usa a tabela
que valia quando ela foi emitida, para que um reajuste da ANTT não mude o
resultado de um compromisso antigo. Para incluir um reajuste, baixe o texto
oficial para docs/fontes/antt, rode scripts/extrair_tabela_antt.py e acrescente
uma TabelaPiso nova em TABELAS (a mais recente por último).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

EIXOS = (2, 3, 4, 5, 6, 7, 9)

TIPOS_CARGA = {
    "carga-geral": "Carga geral",
    "granel-solido": "Granel sólido",
    "granel-liquido": "Granel líquido",
    "frigorificada": "Frigorificada ou aquecida",
    "conteinerizada": "Conteinerizada",
    "neogranel": "Neogranel",
    "perigosa-granel-solido": "Perigosa (granel sólido)",
    "perigosa-granel-liquido": "Perigosa (granel líquido)",
    "perigosa-frigorificada": "Perigosa (frigorificada ou aquecida)",
    "perigosa-conteinerizada": "Perigosa (conteinerizada)",
    "perigosa-carga-geral": "Perigosa (carga geral)",
    "granel-pressurizada": "Carga granel pressurizada",
}
"""As categorias da Tabela A, na ordem e com os nomes da norma."""

_BRASILIA = ZoneInfo("America/Sao_Paulo")


@dataclass(frozen=True)
class TabelaPiso:
    norma: str
    publicacao: str
    vigencia_desde: str  # data (horário de Brasília) em que passa a valer
    url: str
    arquivo_fonte: str   # cópia do texto oficial no repositório
    sha256_fonte: str
    diesel_centavos_por_litro: int
    coeficientes: dict[str, dict[int, tuple[int | None, int | None]]]


TABELAS: tuple[TabelaPiso, ...] = (
    TabelaPiso(
        norma="Portaria SUROC nº 22, de 28 de setembro de 2026 (reajusta o Anexo II da "
              "Resolução ANTT nº 5.867, de 14 de janeiro de 2020)",
        publicacao="D.O.U. de 29/09/2026, Edição Extra 1 A",
        vigencia_desde="2026-09-30",  # art. 3º: no dia seguinte ao da publicação
        url="https://anttlegis.antt.gov.br/action/ActionDatalegis.php?acao=abrirTextoAto"
            "&tipo=POR&numeroAto=00000022&seqAto=000&valorAno=2026&orgao=SUROC%2FANTT%2FMT"
            "&cod_modulo=623&cod_menu=9230",
        arquivo_fonte="docs/fontes/antt/portaria-suroc-22-2026.html",
        sha256_fonte="7a7c0493e8becb84d4e3a28d597a5b7dcebc6bbf47d16aa3527ebf45e7bb5c46",
        diesel_centavos_por_litro=733,  # art. 2º: R$ 7,33/L, diesel S10, média Brasil (ANP)
        coeficientes={'granel-solido': {2: (41056, 46059),
                                       3: (52555, 55224),
                                       4: (59476, 59700),
                                       5: (68548, 66483),
                                       6: (75641, 68001),
                                       7: (82316, 82034),
                                       9: (94318, 90891)},
                     'granel-liquido': {2: (41796, 47198),
                                        3: (53511, 56957),
                                        4: (61019, 62152),
                                        5: (70226, 69308),
                                        6: (77372, 70972),
                                        7: (83700, 84050),
                                        9: (95909, 93476)},
                     'frigorificada': {2: (48234, 52007),
                                       3: (61659, 62327),
                                       4: (70345, 68663),
                                       5: (80623, 75798),
                                       6: (88911, 77235),
                                       7: (98134, 98276),
                                       9: (111479, 106706)},
                     'conteinerizada': {2: (None, None),
                                        3: (52282, 54475),
                                        4: (58755, 57715),
                                        5: (67910, 64729),
                                        6: (74986, 66201),
                                        7: (82292, 81969),
                                        9: (93486, 88605)},
                     'carga-geral': {2: (40738, 45184),
                                     3: (52177, 54186),
                                     4: (59180, 58886),
                                     5: (68284, 65756),
                                     6: (75347, 67193),
                                     7: (82727, 83166),
                                     9: (94114, 90332)},
                     'neogranel': {2: (36743, 45184),
                                   3: (52162, 54144),
                                   4: (59453, 59635),
                                   5: (68284, 65756),
                                   6: (75347, 67193),
                                   7: (82727, 83166),
                                   9: (94114, 90332)},
                     'perigosa-granel-solido': {2: (48756, 60879),
                                                3: (60354, 70316),
                                                4: (67644, 75303),
                                                5: (76715, 82086),
                                                6: (83808, 83604),
                                                7: (90666, 98139),
                                                9: (102747, 107215)},
                     'perigosa-granel-liquido': {2: (49621, 63258),
                                                 3: (61436, 73290),
                                                 4: (68986, 78996),
                                                 5: (78193, 86151),
                                                 6: (85339, 87816),
                                                 7: (91849, 101395),
                                                 9: (104138, 111041)},
                     'perigosa-frigorificada': {2: (54315, 63088),
                                                3: (67869, 73763),
                                                4: (76718, 80763),
                                                5: (86996, 87898),
                                                6: (95284, 89335),
                                                7: (104745, 111028),
                                                9: (118192, 119743)},
                     'perigosa-conteinerizada': {2: (None, None),
                                                 3: (56126, 64545),
                                                 4: (62966, 68295),
                                                 5: (72121, 75310),
                                                 6: (79198, 76781),
                                                 7: (86686, 93051),
                                                 9: (97960, 99906)},
                     'perigosa-carga-geral': {2: (44482, 54981),
                                              3: (56021, 64255),
                                              4: (63392, 69466),
                                              5: (72495, 76336),
                                              6: (79558, 77773),
                                              7: (87121, 94248),
                                              9: (98588, 101633)},
                     'granel-pressurizada': {2: (None, None),
                                             3: (None, None),
                                             4: (None, None),
                                             5: (71929, 75781),
                                             6: (79452, 78482),
                                             7: (None, None),
                                             9: (99531, 105226)}},
    ),
)


class ErroPiso(ValueError):
    """Tipo de carga ou número de eixos fora da Tabela A."""


def tabela_vigente(momento: datetime) -> TabelaPiso | None:
    """A tabela que valia no momento (None se for anterior à primeira guardada)."""
    data = momento.astimezone(_BRASILIA).date().isoformat()
    vigentes = [t for t in TABELAS if t.vigencia_desde <= data]
    return vigentes[-1] if vigentes else None


def piso_centavos(tipo: str, eixos: int, distancia_km: int,
                  tabela: TabelaPiso) -> int | None:
    """Piso da Tabela A em centavos. None se a norma não publica coeficiente para
    essa combinação (células vazias, ex.: conteinerizada com 2 eixos)."""
    if tipo not in TIPOS_CARGA:
        raise ErroPiso(f"tipo de carga fora da Tabela A da ANTT: {tipo!r}")
    if eixos not in EIXOS:
        raise ErroPiso(f"a Tabela A da ANTT não tem coeficiente para {eixos} eixos")
    if not isinstance(distancia_km, int) or distancia_km <= 0:
        raise ErroPiso("a distância precisa ser um número inteiro de km, maior que zero")
    ccd, cc = tabela.coeficientes[tipo][eixos]
    if ccd is None or cc is None:
        return None
    return (distancia_km * ccd + 50) // 100 + cc


def _chave(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return "-".join(re.findall(r"[a-z0-9]+", sem_acento.lower()))


_POR_CHAVE = {**{_chave(nome): slug for slug, nome in TIPOS_CARGA.items()},
              **{slug: slug for slug in TIPOS_CARGA}}


def normalizar_tipo_carga(texto: str) -> str | None:
    """Categoria da ANTT para um texto ("Carga Geral", "carga-geral", "Granel sólido"...).

    None se não for uma das 12 categorias: é o caso de dados antigos em texto livre.
    """
    return _POR_CHAVE.get(_chave(texto))


def nome_tipo_carga(tipo: str) -> str:
    """Nome da categoria para exibir; texto antigo fora da lista volta como veio."""
    slug = normalizar_tipo_carga(tipo)
    return TIPOS_CARGA[slug] if slug else tipo
