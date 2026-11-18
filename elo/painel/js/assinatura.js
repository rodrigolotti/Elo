// Chaves Ed25519 e assinatura no navegador, com a Web Crypto API.
//
// A chave privada é criada NÃO exportável: nem este código consegue tirar os
// bytes dela do navegador. Ela só é usada para assinar, aqui, e nunca vai
// para o servidor. Para o servidor vai só a chave pública (32 bytes em base64).
//
// Espelho de elo/chaves.py: a assinatura cobre os bytes canônicos do objeto
// SEM o campo 'assinatura'.

import { canonicalizar } from "./canonico.js";

export const ALGORITMO = "Ed25519";

export function paraBase64(bytes) {
  let binario = "";
  for (const b of bytes) binario += String.fromCharCode(b);
  return btoa(binario);
}

/** O navegador tem Ed25519 na Web Crypto? (Chrome 137+, Firefox 129+, Safari 17+) */
export async function temEd25519() {
  try {
    await crypto.subtle.generateKey({ name: ALGORITMO }, false, ["sign", "verify"]);
    return true;
  } catch {
    return false;
  }
}

/** Gera um par de chaves com a privada não exportável. Devolve o CryptoKeyPair. */
export async function gerarParDeChaves() {
  return crypto.subtle.generateKey({ name: ALGORITMO }, false, ["sign", "verify"]);
}

/** Chave pública em base64 (32 bytes brutos), no formato que a API espera. */
export async function chavePublicaBase64(chavePublica) {
  return paraBase64(new Uint8Array(await crypto.subtle.exportKey("raw", chavePublica)));
}

/** Devolve uma cópia do objeto com o campo 'assinatura' preenchido. */
export async function assinar(objeto, chavePrivada, chaveId) {
  const semAssinatura = { ...objeto };
  delete semAssinatura.assinatura;
  const assinatura = new Uint8Array(
    await crypto.subtle.sign({ name: ALGORITMO }, chavePrivada, canonicalizar(semAssinatura)));
  return {
    ...semAssinatura,
    assinatura: { alg: ALGORITMO, chave_id: chaveId, valor: paraBase64(assinatura) },
  };
}
