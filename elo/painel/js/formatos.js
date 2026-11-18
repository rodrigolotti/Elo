// Formatos de exibição e de entrada do painel. Sem DOM: testado no Node.
//
// Valores em dinheiro andam sempre como inteiros (centavos), como no protocolo.
// A conversão do que a pessoa digita é feita sobre o texto, nunca com float.

export const UFS = [
  "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT",
  "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO",
];

// O Brasil não tem horário de verão desde 2019: Brasília é sempre -03:00.
export const FUSO_BRASILIA = "-03:00";

function milhares(digitos) {
  return digitos.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
}

/** 485000 -> "R$ 4.850,00" (igual a elo.mandato.reais). */
export function reais(centavos) {
  if (!Number.isSafeInteger(centavos)) throw new Error(`centavos inválidos: ${centavos}`);
  const sinal = centavos < 0 ? "-" : "";
  const absoluto = Math.abs(centavos);
  const inteiro = Math.trunc(absoluto / 100);
  const cent = String(absoluto % 100).padStart(2, "0");
  return `${sinal}R$ ${milhares(String(inteiro))},${cent}`;
}

/** "R$ 6.000,00", "6000", "4.850,5" -> centavos (inteiro). Lança erro se não entender. */
export function centavosDeReais(texto) {
  const limpo = String(texto).replace(/^\s*R\$\s*/, "").trim();
  const casa = /^(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?$/.exec(limpo);
  if (!casa) {
    throw new Error(`valor em reais não reconhecido: "${texto}" (use, por exemplo, 6.000,00)`);
  }
  const inteiro = casa[1].replaceAll(".", "");
  const centavos = Number(inteiro) * 100 + Number((casa[2] ?? "0").padEnd(2, "0"));
  if (!Number.isSafeInteger(centavos)) throw new Error("valor grande demais");
  return centavos;
}

const FORMATO_BRASILIA = new Intl.DateTimeFormat("pt-BR", {
  timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", year: "numeric",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

/** "2026-11-18T00:48:30+00:00" -> "17/11/2026 às 21:48" (horário de Brasília). */
export function emBrasilia(iso) {
  const partes = Object.fromEntries(
    FORMATO_BRASILIA.formatToParts(new Date(iso)).map((p) => [p.type, p.value]));
  return `${partes.day}/${partes.month}/${partes.year} às ${partes.hour}:${partes.minute}`;
}

/** "2026-11-20" -> "20/11/2026" (data sem hora: não há fuso a converter). */
export function dataBr(dataIso) {
  const [ano, mes, dia] = dataIso.split("-");
  return `${dia}/${mes}/${ano}`;
}

/** Valor de <input type="datetime-local"> lido como horário de Brasília -> ISO 8601 com fuso. */
export function isoDeBrasilia(local) {
  const casa = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::(\d{2}))?$/.exec(local);
  if (!casa) throw new Error(`data e hora inválidas: "${local}"`);
  return `${casa[1]}T${casa[2]}:${casa[3] ?? "00"}${FUSO_BRASILIA}`;
}

/** Date -> valor para <input type="datetime-local">, no horário de Brasília. */
export function localDeBrasilia(data) {
  const p = Object.fromEntries(
    FORMATO_BRASILIA.formatToParts(data).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

/** Igual à página de verificação: "SP (00000-000) → PR (11111-111)". */
export function rota(carga) {
  return `${carga.origem_uf} (${carga.origem_cep}) → ${carga.destino_uf} (${carga.destino_cep})`;
}

export function soDigitos(texto) {
  return String(texto).replace(/\D/g, "");
}

/** A chave pública colada é Ed25519 (32 bytes em base64)? */
export function chavePublicaValida(base64) {
  try {
    return atob(base64.trim()).length === 32 && /^[A-Za-z0-9+/]+={0,2}$/.test(base64.trim());
  } catch {
    return false;
  }
}

/** Lista digitada uma por linha ou separada por vírgula, sem vazios nem repetidos. */
export function lista(texto) {
  return [...new Set(String(texto).split(/[\n,]/).map((x) => x.trim()).filter(Boolean))];
}

// ---------- carga e prazo, para a tela de aprovação ----------

/** Fator de cubagem usado no transporte rodoviário: 300 kg por m³. */
export const FATOR_CUBAGEM_KG_POR_M3 = 300;

/** 3200 -> "3.200" */
export function numero(inteiro) {
  return `${inteiro < 0 ? "-" : ""}${milhares(String(Math.abs(Math.trunc(inteiro))))}`;
}

/** 14500 litros -> "14,5 m³" (conta com inteiros, sem float). */
export function metrosCubicos(litros) {
  const inteiro = Math.trunc(litros / 1000);
  const fracao = String(litros % 1000).padStart(3, "0").replace(/0+$/, "");
  return `${numero(inteiro)}${fracao ? `,${fracao}` : ""} m³`;
}

/** Peso cubado em kg (arredondado para cima): litros/1000 × fator. */
export function pesoCubadoKg(litros, fator = FATOR_CUBAGEM_KG_POR_M3) {
  return Math.ceil((litros * fator) / 1000);
}

/** "vence em 23 h", "vence em 45 min", "vence em 3 dias", "venceu há 2 h". */
export function prazoAte(iso, agora = Date.now()) {
  const diferenca = new Date(iso).getTime() - agora;
  const minutos = Math.floor(Math.abs(diferenca) / 60_000);
  const texto = minutos < 60 ? `${Math.max(minutos, 1)} min`
    : minutos < 48 * 60 ? `${Math.floor(minutos / 60)} h`
    : `${Math.floor(minutos / (24 * 60))} dias`;
  return diferenca >= 0 ? `vence em ${texto}` : `venceu há ${texto}`;
}

/** "Frete de R$ 9.000,00 (SP → PR)" */
export function resumoFrete(oferta) {
  return `Frete de ${reais(oferta.condicoes.valor_centavos)} `
    + `(${oferta.carga.origem_uf} → ${oferta.carga.destino_uf})`;
}
