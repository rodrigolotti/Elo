// Faz o papel do navegador nos testes: carrega OS MESMOS módulos do painel
// (elo/painel/js) e responde a comandos JSON, um por linha, em stdin/stdout.
//
// As chaves privadas são geradas aqui, não exportáveis, e ficam só na memória
// deste processo, como no navegador. O Python só recebe chaves públicas e
// objetos assinados.

import { createInterface } from "node:readline";
import { canonicalizar, hashSha256 } from "../../elo/painel/js/canonico.js";
import { assinar, chavePublicaBase64, gerarParDeChaves } from "../../elo/painel/js/assinatura.js";

const chaves = new Map(); // nome -> { par, chaveId }

const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");

const operacoes = {
  async canonico({ valor }) {
    return { hex: hex(canonicalizar(valor)) };
  },
  async hash({ valor }) {
    return { hash: await hashSha256(valor) };
  },
  async gerar({ nome, chave_id }) {
    const par = await gerarParDeChaves();
    chaves.set(nome, { par, chaveId: chave_id });
    return {
      chave_publica: await chavePublicaBase64(par.publicKey),
      privada_extraivel: par.privateKey.extractable,
    };
  },
  async exportar_privada({ nome }) {
    const { par } = chaves.get(nome);
    await crypto.subtle.exportKey("pkcs8", par.privateKey); // deve falhar
    return { exportou: true };
  },
  async assinar({ nome, objeto }) {
    const { par, chaveId } = chaves.get(nome);
    return { assinado: await assinar(objeto, par.privateKey, chaveId) };
  },
};

for await (const linha of createInterface({ input: process.stdin })) {
  let resposta;
  try {
    const comando = JSON.parse(linha);
    resposta = { ok: true, ...(await operacoes[comando.op](comando)) };
  } catch (erro) {
    resposta = { ok: false, erro: `${erro.name}: ${erro.message}` };
  }
  process.stdout.write(JSON.stringify(resposta) + "\n");
}
