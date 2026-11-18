// Confere o rascunho que o servidor devolveu ANTES de assinar.
//
// A chave fica no navegador justamente para que o servidor não consiga agir em
// nome da organização. Então o painel não assina às cegas: cada campo que a
// pessoa escolheu (oferta aprovada, limites do mandato, chave do agente...)
// precisa voltar idêntico no rascunho. Se não voltar, nada é assinado.

import { canonicalizar } from "./canonico.js";

export class ErroConferencia extends Error {
  name = "ErroConferencia";
}

function iguais(a, b) {
  const x = canonicalizar(a);
  const y = canonicalizar(b);
  return x.length === y.length && x.every((byte, i) => byte === y[i]);
}

/** Lança ErroConferencia se algum campo esperado não estiver igual no rascunho. */
export function conferirRascunho(rascunho, esperado) {
  if (rascunho === null || typeof rascunho !== "object" || Array.isArray(rascunho)) {
    throw new ErroConferencia("o servidor não devolveu um rascunho; nada foi assinado");
  }
  if ("assinatura" in rascunho) {
    throw new ErroConferencia("o rascunho já veio com assinatura; nada foi assinado");
  }
  for (const [campo, valor] of Object.entries(esperado)) {
    if (!Object.hasOwn(rascunho, campo) || !iguais(rascunho[campo], valor)) {
      throw new ErroConferencia(
        `o servidor devolveu um rascunho diferente do pedido (campo "${campo}"); ` +
        "nada foi assinado");
    }
  }
  return rascunho;
}
