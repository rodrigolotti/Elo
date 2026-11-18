// Serialização canônica de JSON: espelho de elo/canonico.py (subconjunto da RFC 8785).
//
// Os bytes gerados aqui precisam ser idênticos aos do servidor, senão a
// assinatura feita no navegador não confere. tests/test_painel_js.py roda
// ESTE arquivo no Node e compara byte a byte com o Python.
//
// Regras (as mesmas do Python):
// - chaves ordenadas por unidades UTF-16 (é a ordenação padrão de strings do JS);
// - sem espaços;
// - strings em UTF-8, escapando só aspas, barra invertida e caracteres de controle;
// - números: só inteiros. Além disso, só inteiros "seguros" (até 2^53 - 1):
//   acima disso o JSON.parse do navegador já arredondou o valor e a assinatura
//   sairia sobre um número diferente do que o servidor tem.

export class ErroCanonico extends Error {
  name = "ErroCanonico";
}

const ESCAPES_CURTOS = new Map([
  ['"', '\\"'],
  ["\\", "\\\\"],
  ["\b", "\\b"],
  ["\f", "\\f"],
  ["\n", "\\n"],
  ["\r", "\\r"],
  ["\t", "\\t"],
]);

function exigirUnicodeValido(s) {
  if (!s.isWellFormed()) {
    throw new ErroCanonico(`texto com surrogate UTF-16 solto: ${JSON.stringify(s)}`);
  }
}

function texto(s) {
  exigirUnicodeValido(s);
  let saida = '"';
  for (const ch of s) {
    const cp = ch.codePointAt(0);
    if (ESCAPES_CURTOS.has(ch)) saida += ESCAPES_CURTOS.get(ch);
    else if (cp < 0x20) saida += "\\u" + cp.toString(16).padStart(4, "0");
    else saida += ch;
  }
  return saida + '"';
}

function valor(v) {
  if (v === null) return "null";
  if (v === true) return "true";
  if (v === false) return "false";
  if (typeof v === "number") {
    if (!Number.isInteger(v)) {
      throw new ErroCanonico(
        `número com casa decimal não é permitido (${v}); use inteiro (centavos, gramas, litros)`);
    }
    if (!Number.isSafeInteger(v)) {
      throw new ErroCanonico(`inteiro grande demais para o navegador representar sem perda (${v})`);
    }
    return String(v); // -0 vira "0", como o int do Python
  }
  if (typeof v === "string") return texto(v);
  if (Array.isArray(v)) return "[" + v.map(valor).join(",") + "]";
  if (typeof v === "object" && Object.getPrototypeOf(v) === Object.prototype) {
    const chaves = Object.keys(v);
    chaves.forEach(exigirUnicodeValido);
    chaves.sort(); // compara unidades UTF-16, como sorted(key=utf-16-be) no Python
    return "{" + chaves.map((k) => texto(k) + ":" + valor(v[k])).join(",") + "}";
  }
  throw new ErroCanonico(`tipo não suportado: ${typeof v}`);
}

/** Bytes canônicos (UTF-8) de um objeto JSON. */
export function canonicalizar(obj) {
  return new TextEncoder().encode(valor(obj));
}

/** Hash SHA-256 dos bytes canônicos, no formato 'sha256:<hex>'. */
export async function hashSha256(obj) {
  const resumo = new Uint8Array(await crypto.subtle.digest("SHA-256", canonicalizar(obj)));
  return "sha256:" + Array.from(resumo, (b) => b.toString(16).padStart(2, "0")).join("");
}
