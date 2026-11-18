r"""Gera elo/dados/sedes_municipais_ibge_2022.csv a partir da Base de Localidades do IBGE.

    python scripts\extrair_sedes_ibge.py Localidades_Brasil_shp.zip elo\dados\sedes_municipais_ibge_2022.csv

Baixe antes o zip oficial (SHA-256 1c0bec392891f6f284675e1b72c761f1b4a976599bdd3f0cb87fabf471382ca9):
https://geoftp.ibge.gov.br/organizacao_do_territorio/estrutura_territorial/localidades/Localidades_do_Brasil/2022/Localidades_Brasil_shp.zip

Lê a tabela de atributos (DBF) só com a biblioteca padrão e mantém uma localidade
por município: categoria "Cidade", subcategoria "Sede Municipal" ou "Capital
Federal". Latitude e longitude vão em inteiros (graus × 10⁴).
"""

import csv
import re
import struct
import sys
import zipfile


def ler_dbf(dados: bytes):
    n, cabecalho, tamanho = struct.unpack("<IHH", dados[4:12])
    campos, pos = [], 32
    while dados[pos] != 0x0D:
        campos.append((dados[pos:pos + 11].split(b"\0")[0].decode(), dados[pos + 16]))
        pos += 32
    for i in range(n):
        registro = dados[cabecalho + i * tamanho + 1:cabecalho + (i + 1) * tamanho]
        saida, o = {}, 0
        for nome, largura in campos:
            saida[nome] = registro[o:o + largura].decode("utf-8").strip()
            o += largura
        yield saida


def graus_e4(texto: str) -> int:
    casa = re.fullmatch(r"(-?)(\d+)\.(\d{4})", texto)
    if not casa:
        raise ValueError(f"coordenada fora do formato: {texto!r}")
    return int(casa.group(1) + casa.group(2) + casa.group(3))


def main(zip_ibge: str, saida: str) -> int:
    sedes = {}
    for r in ler_dbf(zipfile.ZipFile(zip_ibge).read("BR_localidades_2022.dbf")):
        if r["CT_LOCALID"] == "Cidade" and r["SCT_LOCALI"] in ("Sede Municipal", "Capital Federal"):
            if r["CD_MUN"] in sedes:
                raise ValueError(f"município com duas sedes: {r['CD_MUN']}")
            sedes[r["CD_MUN"]] = r
    with open(saida, "w", encoding="utf-8", newline="\n") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["cd_mun", "uf", "municipio", "lat_e4", "lon_e4"])
        for codigo in sorted(sedes):
            r = sedes[codigo]
            w.writerow([codigo, r["SIGLA_UF"], r["NM_MUN"], graus_e4(r["LAT_LOCALI"]),
                        graus_e4(r["LONG_LOCAL"])])
    print(f"{len(sedes)} municípios gravados em {saida}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
