"""Estimativa de distância pelo CEP, para quando o agente não informa a distância.

Só informativa: nunca entra na regra do mandato nem na verificação (que precisam
dar sempre o mesmo resultado). No painel, aparece sempre marcada como "estimada".

Como estima:
1. CEP -> município (código IBGE), consultando o ViaCEP (https://viacep.com.br),
   serviço externo gratuito: os CEPs de origem e destino da carga são enviados a
   ele. Timeout curto e cache em memória (falhas ficam em cache por 5 minutos).
2. Município -> coordenadas da sede municipal, da Base de Localidades 2022 do IBGE
   (elo/dados/sedes_municipais_ibge_2022.csv; fonte e extração abaixo).
3. Distância em linha reta entre as sedes (haversine) × 1,25 (fator de
   sinuosidade da estrada), arredondada para km inteiros.

Fonte das coordenadas: IBGE, Localidades do Brasil 2022, arquivo
Localidades_Brasil_shp.zip (SHA-256 1c0bec392891f6f284675e1b72c761f1b4a976599bdd3f0cb87fabf471382ca9),
https://geoftp.ibge.gov.br/organizacao_do_territorio/estrutura_territorial/localidades/Localidades_do_Brasil/2022/
Foram mantidas as localidades da categoria "Cidade" com subcategoria "Sede Municipal"
ou "Capital Federal" (uma por município, 5.570), com latitude e longitude em
inteiros (graus × 10⁴).
"""

from __future__ import annotations

import csv
import json
import math
import re
import time
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

ARQUIVO_SEDES = Path(__file__).parent / "dados" / "sedes_municipais_ibge_2022.csv"
FATOR_SINUOSIDADE_PERCENT = 125
RAIO_TERRA_KM = 6371.0088
TIMEOUT_VIACEP_S = 3
VALIDADE_FALHA_S = 300
METODO = ("estimada: linha reta entre as sedes municipais (IBGE) × 1,25; "
          "município pelo CEP no ViaCEP")


@dataclass(frozen=True)
class Sede:
    codigo_ibge: str
    uf: str
    municipio: str
    lat_e4: int
    lon_e4: int

    @property
    def rotulo(self) -> str:
        return f"{self.municipio}/{self.uf}"


@lru_cache(maxsize=1)
def sedes() -> dict[str, Sede]:
    with ARQUIVO_SEDES.open(encoding="utf-8", newline="") as f:
        return {l["cd_mun"]: Sede(l["cd_mun"], l["uf"], l["municipio"], int(l["lat_e4"]),
                                  int(l["lon_e4"])) for l in csv.DictReader(f)}


def distancia_em_linha_reta_km(a: Sede, b: Sede) -> float:
    la1, lo1, la2, lo2 = (math.radians(v / 10_000) for v in (a.lat_e4, a.lon_e4, b.lat_e4, b.lon_e4))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * RAIO_TERRA_KM * math.asin(math.sqrt(h))


def _cep(texto: str) -> str | None:
    digitos = re.sub(r"\D", "", str(texto))
    return digitos if len(digitos) == 8 else None


def consultar_viacep(cep8: str) -> dict:
    """Consulta real ao ViaCEP (só o CEP vai na requisição)."""
    pedido = urllib.request.Request(f"https://viacep.com.br/ws/{cep8}/json/",
                                    headers={"User-Agent": "elo-prototipo"})
    with urllib.request.urlopen(pedido, timeout=TIMEOUT_VIACEP_S) as resposta:
        return json.loads(resposta.read().decode("utf-8"))


class EstimadorDistancia:
    """Estima a distância entre dois CEPs. A consulta é injetável (testes sem rede)."""

    def __init__(self, consultar: Callable[[str], dict] = consultar_viacep,
                 relogio: Callable[[], float] = time.monotonic) -> None:
        self._consultar = consultar
        self._relogio = relogio
        self._municipio: dict[str, str] = {}               # CEP -> código IBGE
        self._falhas: dict[str, tuple[float, str]] = {}    # CEP -> (quando, motivo)

    def municipio(self, cep: str) -> tuple[Sede | None, str]:
        """(sede, motivo). Sede None quando não deu para descobrir o município."""
        cep8 = _cep(cep)
        if cep8 is None:
            return None, f"CEP inválido: {cep}"
        if cep8 not in self._municipio:
            falha = self._falhas.get(cep8)
            if falha and self._relogio() - falha[0] < VALIDADE_FALHA_S:
                return None, falha[1]
            try:
                dados = self._consultar(cep8)
            except Exception:  # rede, timeout, resposta estranha
                motivo = "o serviço de CEP (ViaCEP) não respondeu"
                self._falhas[cep8] = (self._relogio(), motivo)
                return None, motivo
            if not isinstance(dados, dict) or dados.get("erro") or not dados.get("ibge"):
                motivo = f"CEP {cep} não encontrado no ViaCEP"
                self._falhas[cep8] = (self._relogio(), motivo)
                return None, motivo
            self._municipio[cep8] = str(dados["ibge"])
        sede = sedes().get(self._municipio[cep8])
        if sede is None:
            return None, f"município {self._municipio[cep8]} fora da base do IBGE"
        return sede, ""

    def estimar(self, cep_origem: str, cep_destino: str) -> dict:
        """{"km", "origem", "destino", "metodo"} ou {"km": None, "motivo"}."""
        origem, motivo = self.municipio(cep_origem)
        if origem is None:
            return {"km": None, "motivo": motivo}
        destino, motivo = self.municipio(cep_destino)
        if destino is None:
            return {"km": None, "motivo": motivo}
        reta = distancia_em_linha_reta_km(origem, destino)
        km = max(1, round(reta * FATOR_SINUOSIDADE_PERCENT / 100))
        return {"km": km, "origem": origem.rotulo, "destino": destino.rotulo, "metodo": METODO}
