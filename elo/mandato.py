"""Verificação de uma oferta contra um mandato.

Código puro e determinístico, sem IA. É ele que impede que um e-mail
malicioso convença o agente a oferecer algo fora do autorizado.
"""

from __future__ import annotations

from datetime import datetime

from . import VERSAO_PROTOCOLO, versao_ao_menos
from .antt import (
    TIPOS_CARGA, nome_tipo_carga, normalizar_tipo_carga, piso_centavos, tabela_vigente,
)
from .modelos import Carga, Condicoes, LimitesMandato, ler_momento


def _categoria(tipo: str) -> str:
    # dados antigos em texto livre: compara pela categoria da ANTT quando der
    return normalizar_tipo_carga(tipo) or tipo


AVISO_PISO = "abaixo do piso mínimo da ANTT: risco de multa e indenização"
SEM_DISTANCIA = "distância não informada: não foi possível conferir o piso da ANTT"
SEM_EIXOS = "eixos não informados: não foi possível conferir o piso da ANTT"


def abaixo_do_piso(problemas: list[str]) -> bool:
    return any(p.startswith(AVISO_PISO) for p in problemas)


def piso_da_oferta(carga: Carga, condicoes: Condicoes,
                   momento: datetime) -> dict[str, object] | None:
    """Piso da Tabela A para a oferta, se ela for de carga lotação e trouxer
    distância e eixos. Usa a tabela vigente no momento da emissão. None quando
    não há como calcular (fracionado, dados faltando, categoria fora da lista,
    célula sem coeficiente na norma ou data anterior à primeira tabela)."""
    if (condicoes.modalidade != "lotacao" or condicoes.distancia_km is None
            or condicoes.eixos is None):
        return None
    tabela = tabela_vigente(momento)
    categoria = normalizar_tipo_carga(carga.tipo)
    if tabela is None or categoria is None:
        return None
    piso = piso_centavos(categoria, condicoes.eixos, condicoes.distancia_km, tabela)
    if piso is None:
        return None
    return {"piso_centavos": piso, "categoria": categoria, "nome": TIPOS_CARGA[categoria],
            "eixos": condicoes.eixos, "distancia_km": condicoes.distancia_km,
            "norma": tabela.norma, "vigencia_desde": tabela.vigencia_desde}


def reais(centavos: int) -> str:
    """Formata centavos como R$ 1.234,56."""
    inteiro, cent = divmod(centavos, 100)
    return f"R$ {inteiro:,}".replace(",", ".") + f",{cent:02d}"


def violacoes(
    limites: LimitesMandato,
    carga: Carga,
    condicoes: Condicoes,
    momento: datetime,
    revogado_em: str | None = None,
    versao_protocolo: str | None = VERSAO_PROTOCOLO,
) -> list[str]:
    """Lista os motivos pelos quais a oferta sai do mandato. Vazia = dentro.

    versao_protocolo é a da oferta: a verificação de uma oferta antiga usa as
    regras da versão com que ela foi emitida.
    """
    problemas: list[str] = []

    # protocolo 0.2: em lotação, sem distância ou eixos o piso não pode ser
    # conferido; a oferta não passa direto (vai para aprovação)
    if (condicoes.modalidade == "lotacao" and versao_ao_menos(versao_protocolo, "0.2")
            and tabela_vigente(momento) is not None):
        if condicoes.distancia_km is None:
            problemas.append(SEM_DISTANCIA)
        if condicoes.eixos is None:
            problemas.append(SEM_EIXOS)

    piso = piso_da_oferta(carga, condicoes, momento)
    if piso is not None and condicoes.valor_centavos < piso["piso_centavos"]:
        problemas.append(
            f"{AVISO_PISO} (piso de {reais(piso['piso_centavos'])} para "
            f"{piso['distancia_km']} km, {piso['eixos']} eixos, {piso['nome']}; "
            f"tabela da ANTT vigente desde {piso['vigencia_desde'][8:10]}/"
            f"{piso['vigencia_desde'][5:7]}/{piso['vigencia_desde'][:4]})")

    if revogado_em is not None and ler_momento(revogado_em) <= momento:
        problemas.append(f"mandato revogado em {revogado_em}")
    if momento < ler_momento(limites.valido_de):
        problemas.append(f"mandato só vale a partir de {limites.valido_de}")
    if momento > ler_momento(limites.valido_ate):
        problemas.append(f"mandato venceu em {limites.valido_ate}")
    if condicoes.valor_centavos > limites.valor_max_centavos:
        problemas.append(
            f"valor {reais(condicoes.valor_centavos)} acima do limite de "
            f"{reais(limites.valor_max_centavos)}"
        )
    if carga.origem_uf not in limites.ufs_origem:
        problemas.append(f"origem {carga.origem_uf} fora das UFs autorizadas")
    if carga.destino_uf not in limites.ufs_destino:
        problemas.append(f"destino {carga.destino_uf} fora das UFs autorizadas")
    if _categoria(carga.tipo) not in {_categoria(t) for t in limites.tipos_carga}:
        problemas.append(f"tipo de carga '{nome_tipo_carga(carga.tipo)}' não autorizado")

    return problemas
