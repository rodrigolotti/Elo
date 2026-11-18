import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from elo.chaves import ParDeChaves  # noqa: E402
from elo.cnpj import calcular_digitos, formatar_cnpj  # noqa: E402
from elo.modelos import Carga, Condicoes, LimitesMandato  # noqa: E402
from elo.registro import Registro  # noqa: E402
from elo.servico import Plataforma  # noqa: E402

BRT = timezone(timedelta(hours=-3))


def cnpj_ficticio(base12: str) -> str:
    return formatar_cnpj(base12 + calcular_digitos(base12))


CNPJ_TRANSP = cnpj_ficticio("112223330001")
CNPJ_EMBARC = cnpj_ficticio("223334440001")


class Relogio:
    """Relógio controlável para os testes."""

    def __init__(self, inicio: datetime) -> None:
        self.agora = inicio

    def __call__(self) -> datetime:
        return self.agora

    def avancar(self, **kwargs) -> None:
        self.agora += timedelta(**kwargs)


@pytest.fixture
def relogio():
    return Relogio(datetime(2026, 11, 17, 9, 0, tzinfo=BRT))


@pytest.fixture
def cenario(relogio):
    """Transportadora cadastrada, com agente e mandato, e um pedido registrado."""
    plataforma = Plataforma(Registro(), ParDeChaves.gerar("plataforma#k1"), relogio)
    chave_org = ParDeChaves.gerar("transp#k1")
    chave_agente = ParDeChaves.gerar("agente-cotacao#k1")
    plataforma.cadastrar_organizacao(CNPJ_TRANSP, "Transportadora Fictícia Ltda", chave_org)
    agente = plataforma.emitir_agente(CNPJ_TRANSP, chave_org, chave_agente, "Agente de cotação")
    limites = LimitesMandato(
        valor_max_centavos=600_000,
        ufs_origem=["SP"],
        ufs_destino=["SP", "PR"],
        tipos_carga=["carga geral"],
        valido_de="2026-11-01T00:00:00-03:00",
        valido_ate="2026-12-31T23:59:59-03:00",
    )
    mandato = plataforma.emitir_mandato(CNPJ_TRANSP, chave_org, agente["agente_id"], limites)
    pedido = plataforma.registrar_pedido(
        CNPJ_EMBARC, "email",
        Carga(origem_cep="00000-000", origem_uf="SP", destino_cep="11111-111",
              destino_uf="PR", peso_kg=3200, volume_litros=14500, tipo="carga geral"),
        "Bom dia, preciso de cotação para 3,2 t de SP para Curitiba.",
    )
    return {
        "plataforma": plataforma,
        "chave_org": chave_org,
        "chave_agente": chave_agente,
        "agente_id": agente["agente_id"],
        "mandato_id": mandato["id"],
        "pedido_id": pedido["id"],
        "relogio": relogio,
    }


def condicoes(valor_centavos: int = 485_000) -> Condicoes:
    # fracionada: os testes que não tratam do piso da ANTT ficam fora dessa regra
    return Condicoes(valor_centavos=valor_centavos, coleta_ate="2026-11-20",
                     prazo_entrega_dias_uteis=2, inclui=["pedagio", "seguro_rctr_c"],
                     modalidade="fracionada")
