"""Estimativa de distância pelo CEP (ViaCEP + sedes municipais do IBGE). Sem rede:
a consulta ao ViaCEP é trocada por uma falsa."""

import csv

import pytest

from elo.distancia import ARQUIVO_SEDES, VALIDADE_FALHA_S, EstimadorDistancia, sedes

IBGE = {"01001000": "3550308", "80010000": "4106902", "70040010": "5300108"}


class ViaCepFalso:
    def __init__(self, falhar=False):
        self.chamadas = []
        self.falhar = falhar

    def __call__(self, cep8):
        self.chamadas.append(cep8)
        if self.falhar:
            raise TimeoutError("demorou")
        return {"ibge": IBGE[cep8]} if cep8 in IBGE else {"erro": True}


class Relogio:
    agora = 0.0

    def __call__(self):
        return self.agora


def test_base_do_ibge_tem_os_5570_municipios():
    with ARQUIVO_SEDES.open(encoding="utf-8", newline="") as f:
        linhas = list(csv.DictReader(f))
    assert len(linhas) == len({l["cd_mun"] for l in linhas}) == 5570
    assert sedes()["5300108"].rotulo == "Brasília/DF"
    assert sedes()["3550308"].rotulo == "São Paulo/SP"


def test_estimativa_entre_capitais():
    e = EstimadorDistancia(ViaCepFalso())
    r = e.estimar("01001-000", "80010-000")
    assert r["km"] == 427 and r["origem"] == "São Paulo/SP" and r["destino"] == "Curitiba/PR"
    assert r["metodo"].startswith("estimada")
    assert e.estimar("01001000", "70040-010")["km"] == 1094


def test_cada_cep_e_consultado_uma_vez_so():
    via = ViaCepFalso()
    e = EstimadorDistancia(via)
    for _ in range(3):
        e.estimar("01001-000", "80010-000")
    assert sorted(via.chamadas) == ["01001000", "80010000"]


def test_cep_invalido_ou_desconhecido_explica_o_motivo():
    e = EstimadorDistancia(ViaCepFalso())
    assert e.estimar("123", "80010-000") == {"km": None, "motivo": "CEP inválido: 123"}
    assert e.estimar("99999-999", "80010-000")["motivo"] == "CEP 99999-999 não encontrado no ViaCEP"


def test_viacep_fora_do_ar_nao_derruba_e_tenta_de_novo_depois():
    via, relogio = ViaCepFalso(falhar=True), Relogio()
    e = EstimadorDistancia(via, relogio)
    assert e.estimar("01001-000", "80010-000")["motivo"] == "o serviço de CEP (ViaCEP) não respondeu"
    e.estimar("01001-000", "80010-000")
    assert via.chamadas == ["01001000"]          # a falha fica em cache
    relogio.agora = VALIDADE_FALHA_S + 1
    via.falhar = False
    assert e.estimar("01001-000", "80010-000")["km"] == 427


def test_municipio_fora_da_base():
    e = EstimadorDistancia(lambda cep: {"ibge": "0000000"})
    assert "fora da base do IBGE" in e.estimar("01001-000", "80010-000")["motivo"]
