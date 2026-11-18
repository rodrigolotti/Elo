"""Formatos validados do conteúdo das mensagens.

Tudo em inteiros: valores em centavos, peso em quilos, volume em litros.
Datas e horários em ISO 8601 com fuso (ex.: 2026-11-18T18:00:00-03:00).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

UF = Literal[
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT",
    "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO",
]

CEP = Field(pattern=r"^\d{5}-\d{3}$")


def ler_momento(texto: str) -> datetime:
    """Converte ISO 8601 em datetime e exige fuso horário."""
    momento = datetime.fromisoformat(texto)
    if momento.tzinfo is None:
        raise ValueError(f"data/hora sem fuso horário: {texto}")
    return momento


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Carga(_Base):
    origem_cep: str = CEP
    origem_uf: UF
    destino_cep: str = CEP
    destino_uf: UF
    peso_kg: int = Field(gt=0)
    volume_litros: int = Field(ge=0)
    tipo: str = Field(min_length=1, max_length=60)


class Condicoes(_Base):
    valor_centavos: int = Field(gt=0)
    coleta_ate: date
    prazo_entrega_dias_uteis: int = Field(ge=0, le=60)
    inclui: list[str] = Field(default_factory=list)
    # Informados e assinados pelo agente; opcionais (ofertas antigas não têm).
    # O piso mínimo da ANTT (Tabela A) só vale para carga lotação.
    modalidade: Literal["lotacao", "fracionada"] | None = None
    distancia_km: int | None = Field(default=None, gt=0, le=10_000,
                                     description="Distância da ida, em km inteiros")
    eixos: Literal[2, 3, 4, 5, 6, 7, 9] | None = Field(
        default=None, description="Eixos carregados do veículo, como na Tabela A da ANTT")


class LimitesMandato(_Base):
    """O que o agente pode oferecer sozinho, sem aprovação humana."""

    valor_max_centavos: int = Field(gt=0)
    ufs_origem: list[UF] = Field(min_length=1)
    ufs_destino: list[UF] = Field(min_length=1)
    tipos_carga: list[str] = Field(min_length=1)
    valido_de: str
    valido_ate: str

    @field_validator("valido_de", "valido_ate")
    @classmethod
    def _com_fuso(cls, v: str) -> str:
        ler_momento(v)
        return v

    @model_validator(mode="after")
    def _periodo(self) -> "LimitesMandato":
        if ler_momento(self.valido_ate) <= ler_momento(self.valido_de):
            raise ValueError("valido_ate precisa ser depois de valido_de")
        return self
