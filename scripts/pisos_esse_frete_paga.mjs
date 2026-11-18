// Calcula, com o código do projeto esse-frete-paga, o piso da ANTT para uma grade
// de tipos de carga, eixos e distâncias, e imprime em JSON. Serve para conferir
// que o Elo e o esse-frete-paga dão o mesmo piso para os mesmos dados.
//
//   node scripts/pisos_esse_frete_paga.mjs <pasta-do-esse-frete-paga> [commit]
//
// tests/dados/esse_frete_paga.json foi gerado assim; tests/test_antt.py compara.
// Se a variável ESSE_FRETE_PAGA apontar para uma cópia do projeto, o teste
// também roda este script na hora.

import { pathToFileURL } from "node:url";
import { join, resolve } from "node:path";

const pasta = resolve(process.argv[2] ?? "");
const commit = process.argv[3] ?? null;
const { pisoAntt } = await import(pathToFileURL(join(pasta, "src", "calc.js")).href);
const { TABELA_A, TABELA_INFO, EIXOS } = await import(
  pathToFileURL(join(pasta, "src", "antt-tabela-a.js")).href);

export const DISTANCIAS = [1, 7, 10, 99, 100, 333, 500, 777, 1234, 2999];

const pisos = [];
for (const tipoCarga of Object.keys(TABELA_A)) {
  for (const eixos of EIXOS) {
    for (const distanciaKm of DISTANCIAS) {
      const r = pisoAntt({ tipoCarga, eixos, distanciaKm });
      // em centavos inteiros, para comparar sem float
      pisos.push({ tipoCarga, eixos, distanciaKm,
                   pisoCentavos: r === null ? null : Math.round(r.piso * 100) });
    }
  }
}
process.stdout.write(JSON.stringify({ commit, tabelaInfo: TABELA_INFO, eixos: EIXOS,
                                      tabelaA: TABELA_A, pisos }, null, 1) + "\n");
