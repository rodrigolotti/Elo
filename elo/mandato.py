"""Verificação de uma oferta contra um mandato.

Código puro e determinístico, sem IA. É ele que impede que um e-mail
malicioso convença o agente a oferecer algo fora do autorizado.
"""

from __future__ import annotations

from datetime import datetime

from .modelos import Carga, Condicoes, LimitesMandato, ler_momento


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
) -> list[str]:
    """Lista os motivos pelos quais a oferta sai do mandato. Vazia = dentro."""
    problemas: list[str] = []

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
    if carga.tipo not in limites.tipos_carga:
        problemas.append(f"tipo de carga '{carga.tipo}' não autorizado")

    return problemas
