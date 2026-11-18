// Cliente da API do Elo para o painel.
//
// Tudo que precisa da organização segue o fluxo da API: pede o rascunho,
// confere o rascunho, assina aqui no navegador e envia o objeto assinado.

import { assinar } from "./assinatura.js";
import { lerChave } from "./cofre.js";
import { conferirRascunho } from "./conferencia.js";

const CHAVE_SESSAO = "elo.sessao";

export class ErroApi extends Error {
  name = "ErroApi";
  constructor(status, mensagem) {
    super(mensagem);
    this.status = status;
  }
}

// ---------- sessão (só nesta aba; o token dura 30 minutos) ----------

export function sessao() {
  try {
    return JSON.parse(sessionStorage.getItem(CHAVE_SESSAO));
  } catch {
    return null;
  }
}

function guardarSessao(dados) {
  sessionStorage.setItem(CHAVE_SESSAO, JSON.stringify(dados));
}

/** Atualiza o nome mostrado no topo (depois de uma recuperação). */
export function definirRazaoSocial(razaoSocial) {
  const atual = sessao();
  if (atual) guardarSessao({ ...atual, razaoSocial });
}

export function esquecerSessao() {
  sessionStorage.removeItem(CHAVE_SESSAO);
}

// ---------- HTTP ----------

function mensagemDeErro(status, corpo) {
  const detalhe = corpo?.detail;
  if (Array.isArray(detalhe)) {  // 422 do FastAPI: lista de campos inválidos
    return detalhe.map((d) => `${(d.loc ?? []).slice(1).join(".") || "corpo"}: ${d.msg}`)
      .join("; ");
  }
  return detalhe ?? `erro ${status}`;
}

async function pedir(metodo, rota, corpo) {
  const cabecalhos = {};
  const atual = sessao();
  if (atual) cabecalhos.Authorization = `Bearer ${atual.token}`;
  if (corpo !== undefined) cabecalhos["Content-Type"] = "application/json";
  let resposta;
  try {
    resposta = await fetch(rota, {
      method: metodo, headers: cabecalhos,
      body: corpo === undefined ? undefined : JSON.stringify(corpo),
    });
  } catch {
    throw new ErroApi(0, "não foi possível falar com o servidor do Elo");
  }
  const dados = resposta.status === 204 ? null : await resposta.json().catch(() => null);
  if (!resposta.ok) {
    if (resposta.status === 401 && atual) {
      esquecerSessao();
      window.dispatchEvent(new CustomEvent("elo:sessao-expirada"));
    }
    throw new ErroApi(resposta.status, mensagemDeErro(resposta.status, dados));
  }
  return dados;
}

export const obter = (rota) => pedir("GET", rota);
export const enviar = (rota, corpo) => pedir("POST", rota, corpo);

// ---------- rascunho -> conferência -> assinatura -> envio ----------

async function registroDaSessao() {
  const atual = sessao();
  const registro = atual && (await lerChave(atual.cnpj));
  if (!registro) throw new ErroApi(0, "a chave desta organização não está neste navegador");
  return registro;
}

/**
 * Pede o rascunho, confere os campos esperados, assina com a chave da
 * organização e envia. Devolve a resposta do envio.
 */
export async function assinarEEnviar({ rotaRascunho, corpo, rotaEnvio, esperado,
                                       registro }) {
  const chave = registro ?? (await registroDaSessao());
  const { rascunho } = await enviar(rotaRascunho, corpo);
  conferirRascunho(rascunho, esperado);
  return enviar(rotaEnvio, await assinar(rascunho, chave.privada, chave.chaveId));
}

/** Entra no painel assinando o desafio com a chave guardada da organização. */
export async function entrar(registro) {
  const { desafio } = await enviar("/sessoes/desafio", { cnpj: registro.cnpj });
  conferirRascunho(desafio, { tipo: "desafio_sessao", cnpj: registro.cnpj });
  const resposta = await enviar("/sessoes",
    await assinar(desafio, registro.privada, registro.chaveId));
  guardarSessao({ token: resposta.token, cnpj: resposta.cnpj,
                  razaoSocial: registro.razaoSocial, expiraEm: resposta.expira_em });
  return resposta;
}

export async function sair() {
  try {
    await pedir("DELETE", "/sessoes");
  } finally {
    esquecerSessao();
  }
}
