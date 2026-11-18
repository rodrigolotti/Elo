// Arquivo de recuperação da chave da organização.
//
// A chave de recuperação é um segundo par Ed25519 que só serve para trocar a
// chave principal (evento chave_rotacionada). A parte privada sai daqui uma
// única vez, cifrada, num arquivo que a pessoa guarda fora do navegador. A
// frase-senha que abre o arquivo é gerada aqui e anotada à parte.
//
// Formato (elo/recuperacao.py lê e grava o mesmo):
//   PBKDF2-HMAC-SHA256 (sal de 16 bytes) -> chave AES-256 -> AES-GCM (IV de 12 bytes)
//   texto claro: a chave privada em PKCS#8
//   dado autenticado (AAD): o cabeçalho canônico (CNPJ, chave, parâmetros do KDF),
//   então alterar qualquer um deles, inclusive baixar as iterações, impede a abertura.

import { canonicalizar } from "./canonico.js";
import { ALGORITMO, chavePublicaBase64, paraBase64 } from "./assinatura.js";

// OWASP Password Storage Cheat Sheet, seção PBKDF2:
// "PBKDF2-HMAC-SHA256: 600,000 iterations"
// https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
// (a OWASP prefere Argon2id, mas a Web Crypto do navegador só oferece PBKDF2)
export const ITERACOES_PBKDF2 = 600_000;

export const FORMATO = "elo-recuperacao";
export const VERSAO = 1;
const ALG_KDF = "PBKDF2-HMAC-SHA256";
const ALG_CIFRA = "AES-256-GCM";
const BYTES_SAL = 16;
const BYTES_IV = 12;

// Crockford base32: sem I, L, O e U, para não confundir na hora de anotar
const ALFABETO = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const TAMANHO_FRASE = 25; // 25 x 5 bits = 125 bits

export class ErroRecuperacao extends Error {
  name = "ErroRecuperacao";
}

// ---------- frase-senha ----------

/** Frase aleatória de 125 bits, em 5 grupos: "7KQ2M-..." */
export function gerarFraseSenha() {
  const bytes = crypto.getRandomValues(new Uint8Array(TAMANHO_FRASE));
  const letras = Array.from(bytes, (b) => ALFABETO[b & 31]); // 256 é múltiplo de 32: sem viés
  return [0, 5, 10, 15, 20].map((i) => letras.slice(i, i + 5).join("")).join("-");
}

/** Aceita minúsculas, espaços e hífens; O vira 0, I e L viram 1. */
export function normalizarFrase(texto) {
  const limpa = String(texto).toUpperCase().replace(/[\s-]/g, "")
    .replaceAll("O", "0").replace(/[IL]/g, "1");
  if (limpa.length !== TAMANHO_FRASE || [...limpa].some((c) => !ALFABETO.includes(c))) {
    throw new ErroRecuperacao(
      `a frase-senha tem ${TAMANHO_FRASE} letras e números, em 5 grupos (ex.: 7KQ2M-...); `
      + "confira o que foi digitado");
  }
  return limpa;
}

// ---------- base64 estrito ----------

function deBase64(texto, campo, tamanho) {
  if (typeof texto !== "string" || !/^[A-Za-z0-9+/]*={0,2}$/.test(texto) || texto.length % 4) {
    throw new ErroRecuperacao(`arquivo de recuperação corrompido: campo "${campo}" inválido`);
  }
  const bytes = Uint8Array.from(atob(texto), (c) => c.charCodeAt(0));
  if (tamanho !== undefined && bytes.length !== tamanho) {
    throw new ErroRecuperacao(`arquivo de recuperação corrompido: campo "${campo}" com tamanho errado`);
  }
  return bytes;
}

// ---------- cifra ----------

function cabecalho(arquivo) {
  // o que fica autenticado junto com a chave cifrada
  const { formato, versao, cnpj, chave_id, chave_publica, kdf } = arquivo;
  return canonicalizar({ formato, versao, cnpj, chave_id, chave_publica,
                         kdf: { alg: kdf.alg, iteracoes: kdf.iteracoes, sal: kdf.sal } });
}

async function chaveDaFrase(frase, sal, iteracoes) {
  const material = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(normalizarFrase(frase)), "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", hash: "SHA-256", salt: sal, iterations: iteracoes },
    material, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
}

/**
 * Gera a chave de recuperação e o arquivo cifrado. A parte privada só existe
 * em memória até ser cifrada; nada dela volta para quem chama.
 * Devolve { arquivo, chavePublica }.
 */
export async function criarArquivoRecuperacao({ cnpj, chaveId }, frase) {
  // exportável só aqui, porque precisa ser cifrada para o arquivo
  const par = await crypto.subtle.generateKey({ name: ALGORITMO }, true, ["sign", "verify"]);
  const chavePublica = await chavePublicaBase64(par.publicKey);
  const pkcs8 = new Uint8Array(await crypto.subtle.exportKey("pkcs8", par.privateKey));
  const sal = crypto.getRandomValues(new Uint8Array(BYTES_SAL));
  const iv = crypto.getRandomValues(new Uint8Array(BYTES_IV));
  const arquivo = {
    formato: FORMATO, versao: VERSAO, cnpj, chave_id: chaveId, chave_publica: chavePublica,
    kdf: { alg: ALG_KDF, iteracoes: ITERACOES_PBKDF2, sal: paraBase64(sal) },
    cifra: { alg: ALG_CIFRA, iv: paraBase64(iv), dados: "" },
    criado_em: new Date().toISOString(),
  };
  const chave = await chaveDaFrase(frase, sal, ITERACOES_PBKDF2);
  const cifrado = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: cabecalho(arquivo) }, chave, pkcs8);
  pkcs8.fill(0);
  arquivo.cifra.dados = paraBase64(new Uint8Array(cifrado));
  return { arquivo, chavePublica };
}

function conferirEstrutura(arquivo) {
  if (arquivo === null || typeof arquivo !== "object" || arquivo.formato !== FORMATO) {
    throw new ErroRecuperacao("este arquivo não é um arquivo de recuperação do Elo");
  }
  if (arquivo.versao !== VERSAO) {
    throw new ErroRecuperacao(`versão ${arquivo.versao} do arquivo de recuperação não é suportada`);
  }
  for (const campo of ["cnpj", "chave_id", "chave_publica"]) {
    if (typeof arquivo[campo] !== "string" || !arquivo[campo]) {
      throw new ErroRecuperacao(`arquivo de recuperação corrompido: campo "${campo}" ausente`);
    }
  }
  const { kdf, cifra } = arquivo;
  if (kdf?.alg !== ALG_KDF || cifra?.alg !== ALG_CIFRA) {
    throw new ErroRecuperacao("arquivo de recuperação com algoritmo desconhecido");
  }
  if (!Number.isSafeInteger(kdf.iteracoes) || kdf.iteracoes < ITERACOES_PBKDF2) {
    throw new ErroRecuperacao(
      `o arquivo pede ${kdf.iteracoes} iterações, abaixo do mínimo de ${ITERACOES_PBKDF2}: `
      + "foi alterado ou não é confiável");
  }
  deBase64(arquivo.chave_publica, "chave_publica", 32);
  return {
    sal: deBase64(kdf.sal, "kdf.sal", BYTES_SAL),
    iv: deBase64(cifra.iv, "cifra.iv", BYTES_IV),
    dados: deBase64(cifra.dados, "cifra.dados"),
  };
}

/**
 * Abre o arquivo com a frase-senha. Devolve { chavePrivada (CryptoKey NÃO
 * exportável), chaveId, chavePublica, cnpj }. Erros: ErroRecuperacao com
 * mensagem para mostrar à pessoa.
 */
export async function abrirArquivoRecuperacao(arquivo, frase, cnpjEsperado) {
  const { sal, iv, dados } = conferirEstrutura(arquivo);
  if (cnpjEsperado !== undefined && arquivo.cnpj !== cnpjEsperado) {
    throw new ErroRecuperacao(
      `este arquivo de recuperação é da organização ${arquivo.cnpj}, não de ${cnpjEsperado}`);
  }
  const chave = await chaveDaFrase(frase, sal, arquivo.kdf.iteracoes);
  let pkcs8;
  try {
    pkcs8 = new Uint8Array(await crypto.subtle.decrypt(
      { name: "AES-GCM", iv, additionalData: cabecalho(arquivo) }, chave, dados));
  } catch {
    // o AES-GCM não distingue os dois casos, e a mensagem não finge que distingue
    throw new ErroRecuperacao(
      "não foi possível abrir o arquivo: a frase-senha está errada ou o arquivo foi alterado");
  }
  try {
    // confere que a chave decifrada é a da chave pública declarada: para isso
    // importa uma vez como exportável (só para ler a parte pública, em JWK)...
    const conferir = await crypto.subtle.importKey("pkcs8", pkcs8, { name: ALGORITMO }, true, ["sign"]);
    const { x } = await crypto.subtle.exportKey("jwk", conferir);
    const publica = paraBase64(Uint8Array.from(
      atob(x.replace(/-/g, "+").replace(/_/g, "/") + "="), (c) => c.charCodeAt(0)));
    if (publica !== arquivo.chave_publica) {
      throw new ErroRecuperacao("arquivo adulterado: a chave cifrada não corresponde à chave pública declarada");
    }
    // ...e a que fica em uso é NÃO exportável
    const chavePrivada = await crypto.subtle.importKey(
      "pkcs8", pkcs8, { name: ALGORITMO }, false, ["sign"]);
    return { chavePrivada, chaveId: arquivo.chave_id, chavePublica: publica, cnpj: arquivo.cnpj };
  } catch (erro) {
    if (erro instanceof ErroRecuperacao) throw erro;
    throw new ErroRecuperacao("arquivo de recuperação corrompido: a chave cifrada não é Ed25519");
  } finally {
    pkcs8.fill(0);
  }
}

/** Nome sugerido para o download. */
export function nomeDoArquivo(cnpj) {
  return `elo-recuperacao-${String(cnpj).replace(/\D/g, "")}.json`;
}
